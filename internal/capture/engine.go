package capture

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const Version = "0.5.0"

const silverScale int64 = 10_000

type OperationCodes struct {
	Join                           uint16
	GetGameServer                  uint16
	AuctionGetOffers               uint16
	AuctionGetRequests             uint16
	AuctionBuyOffer                uint16
	AuctionSellRequest             uint16
	AuctionSellSpecificItemRequest uint16
	QuickSellQuery                 uint16
	QuickSellAction                uint16
	GetMailInfos                   uint16
	ReadMail                       uint16
}

func DefaultOperationCodes() OperationCodes {
	return OperationCodes{
		Join: 2, GetGameServer: 17, AuctionGetOffers: 81, AuctionGetRequests: 82,
		AuctionBuyOffer: 83, AuctionSellRequest: 88, AuctionSellSpecificItemRequest: 315,
		QuickSellQuery: 484, QuickSellAction: 485,
		GetMailInfos: 174, ReadMail: 176,
	}
}

type MarketOrder struct {
	ID               int64  `json:"Id"`
	ItemID           string `json:"ItemTypeId"`
	LocationID       string `json:"LocationId"`
	QualityLevel     int    `json:"QualityLevel"`
	EnchantmentLevel int    `json:"EnchantmentLevel"`
	UnitPrice        int64  `json:"UnitPriceSilver"`
	Amount           int64  `json:"Amount"`
	AuctionType      string `json:"AuctionType"`
	Expires          string `json:"Expires"`
}

type Event struct {
	Type             string `json:"type"`
	SourceEventID    string `json:"source_event_id,omitempty"`
	CapturedAt       string `json:"captured_at"`
	TradedAt         string `json:"traded_at,omitempty"`
	PurchasedAt      string `json:"purchased_at,omitempty"`
	Direction        string `json:"direction,omitempty"`
	TransactionKind  string `json:"transaction_kind,omitempty"`
	ItemID           string `json:"item_id,omitempty"`
	Quantity         int64  `json:"quantity,omitempty"`
	UnitPrice        int64  `json:"unit_price,omitempty"`
	TotalPrice       int64  `json:"total_price,omitempty"`
	OrderID          string `json:"order_id,omitempty"`
	LocationID       string `json:"location_id,omitempty"`
	CharacterName    string `json:"character_name,omitempty"`
	GameServer       string `json:"game_server,omitempty"`
	QualityLevel     int    `json:"quality_level,omitempty"`
	EnchantmentLevel int    `json:"enchantment_level,omitempty"`
	Source           string `json:"source,omitempty"`
	Confidence       string `json:"confidence,omitempty"`
	Code             string `json:"code,omitempty"`
	Message          string `json:"message,omitempty"`
	RawParams        string `json:"raw_params,omitempty"`
	MailID           int64  `json:"mail_id,omitempty"`
	MailType         string `json:"mail_type,omitempty"`
	MailReceived     int64  `json:"mail_received,omitempty"`
	MailState        string `json:"mail_state,omitempty"`
}

type Status struct {
	LocationID    string
	CharacterName string
	LastPacketAt  time.Time
	LastEncrypted time.Time
	PacketsSeen   uint64
}

type pendingTrade struct {
	Order       MarketOrder
	Quantity    int64
	RequestedAt time.Time
	Confidence  string
}

type mailInfo struct {
	LocationID string
	OrderType  string
	Received   int64
}

type Engine struct {
	mu               sync.Mutex
	codes            OperationCodes
	emit             func(Event)
	offers           map[int64]MarketOrder
	requests         map[int64]MarketOrder
	pending          map[uint16][]pendingTrade
	recentRequests   map[string]time.Time
	mailInfos        map[int64]mailInfo
	quickSellOrders  []MarketOrder
	pendingQuickSell [][]pendingTrade
	locationID       string
	characterName    string
	lastPacketAt     time.Time
	lastEncrypted    time.Time
	packetsSeen      uint64
	orderIDParam     byte
	quantityParam    byte
	warningThrottles map[string]time.Time
}

func NewEngine(codes OperationCodes, orderIDParam, quantityParam byte, emit func(Event)) *Engine {
	return &Engine{
		codes:            codes,
		emit:             emit,
		offers:           make(map[int64]MarketOrder),
		requests:         make(map[int64]MarketOrder),
		pending:          make(map[uint16][]pendingTrade),
		recentRequests:   make(map[string]time.Time),
		mailInfos:        make(map[int64]mailInfo),
		pendingQuickSell: make([][]pendingTrade, 0),
		orderIDParam:     orderIDParam,
		quantityParam:    quantityParam,
		warningThrottles: make(map[string]time.Time),
	}
}

func (e *Engine) PacketSeen(now time.Time) {
	e.mu.Lock()
	e.lastPacketAt = now
	e.packetsSeen++
	e.mu.Unlock()
}

func (e *Engine) Encrypted(now time.Time) {
	e.mu.Lock()
	e.lastEncrypted = now
	e.mu.Unlock()
	e.warning("encrypted_market_data", "偵測到加密的 Photon 資料；若市場無法記錄，請更新捕捉器/AODP 解析器", "")
}

func (e *Engine) Snapshot() Status {
	e.mu.Lock()
	defer e.mu.Unlock()
	return Status{
		LocationID: e.locationID, CharacterName: e.characterName,
		LastPacketAt: e.lastPacketAt, LastEncrypted: e.lastEncrypted,
		PacketsSeen: e.packetsSeen,
	}
}

func (e *Engine) HandleRequest(opCode byte, params map[byte]interface{}, now time.Time) {
	code := resolveCode(opCode, params)
	e.mu.Lock()
	defer e.mu.Unlock()

	if code == e.codes.GetGameServer {
		if location, ok := params[0].(string); ok && location != "" {
			e.locationID = normalizeLocation(location)
		}
	}
	if code == e.codes.QuickSellAction {
		e.pruneLocked(now)
		quantity, confidence := e.findQuickSellQuantityLocked(params)
		if quantity <= 0 || len(e.quickSellOrders) == 0 {
			go e.warning("quick_sell_not_cached", "收到快速出售，但尚未取得可用買單；請先開啟市場出售畫面再重試", formatParams(params))
			return
		}
		remaining := quantity
		var trades []pendingTrade
		for _, order := range e.quickSellOrders {
			if remaining <= 0 {
				break
			}
			amount := order.Amount
			if amount > remaining {
				amount = remaining
			}
			trades = append(trades, pendingTrade{Order: order, Quantity: amount, RequestedAt: now, Confidence: confidence})
			remaining -= amount
		}
		if remaining > 0 {
			go e.warning("quick_sell_quantity_exceeds_cache", "快速出售數量超過已取得的買單數量，本次未入帳以避免金額錯誤", formatParams(params))
			return
		}
		e.pendingQuickSell = append(e.pendingQuickSell, trades)
		return
	}
	if code != e.codes.AuctionBuyOffer && code != e.codes.AuctionSellRequest && code != e.codes.AuctionSellSpecificItemRequest {
		return
	}

	e.pruneLocked(now)
	orders := e.offers
	direction := "buy"
	if code == e.codes.AuctionSellRequest || code == e.codes.AuctionSellSpecificItemRequest {
		orders = e.requests
		direction = "sell"
	}
	order, found := e.findOrderLocked(params, orders)
	if !found {
		go e.warning(direction+"_order_not_cached", "收到成交請求，但找不到先前市場清單中的訂單；本次未入帳", formatParams(params))
		return
	}

	quantity, confidence := e.findQuantityLocked(params, order)
	fingerprint := fmt.Sprintf("%d:%d:%d", code, order.ID, quantity)
	if previous, exists := e.recentRequests[fingerprint]; exists && now.Sub(previous) < time.Second {
		return
	}
	e.recentRequests[fingerprint] = now
	e.pending[code] = append(e.pending[code], pendingTrade{
		Order: order, Quantity: quantity, RequestedAt: now, Confidence: confidence,
	})
}

func (e *Engine) HandleResponse(opCode byte, returnCode int16, params map[byte]interface{}, now time.Time) {
	code := resolveCode(opCode, params)

	if rawOrders := orderStrings(params); len(rawOrders) > 0 {
		if code == e.codes.AuctionGetOffers {
			e.cacheOrders(rawOrders, false)
		} else if code == e.codes.AuctionGetRequests {
			e.cacheOrders(rawOrders, true)
		} else if code == e.codes.QuickSellQuery {
			e.cacheQuickSellOrders(rawOrders)
		}
	}
	if code == e.codes.GetMailInfos && returnCode == 0 {
		e.cacheMailInfos(params)
	}
	if code == e.codes.ReadMail && returnCode == 0 {
		e.handleReadMail(params, now)
	}

	e.mu.Lock()
	if code == e.codes.Join {
		if name, ok := params[2].(string); ok && name != "" {
			e.characterName = name
		}
		if location, ok := params[8].(string); ok && location != "" {
			e.locationID = normalizeLocation(location)
		}
	}
	if code == e.codes.QuickSellAction {
		if len(e.pendingQuickSell) == 0 {
			e.mu.Unlock()
			return
		}
		trades := e.pendingQuickSell[0]
		e.pendingQuickSell = e.pendingQuickSell[1:]
		if returnCode != 0 {
			e.mu.Unlock()
			return
		}
		character, fallbackLocation := e.characterName, e.locationID
		for _, trade := range trades {
			if cached, ok := e.requests[trade.Order.ID]; ok {
				cached.Amount -= trade.Quantity
				if cached.Amount <= 0 {
					delete(e.requests, trade.Order.ID)
				} else {
					e.requests[trade.Order.ID] = cached
				}
			}
		}
		e.mu.Unlock()
		for _, trade := range trades {
			e.emitTrade(trade, "sell", "quick_sell", character, fallbackLocation, now)
		}
		return
	}
	if code != e.codes.AuctionBuyOffer && code != e.codes.AuctionSellRequest && code != e.codes.AuctionSellSpecificItemRequest {
		e.mu.Unlock()
		return
	}

	e.pruneLocked(now)
	pendingTrades := e.pending[code]
	if len(pendingTrades) == 0 {
		e.mu.Unlock()
		return
	}
	pending := pendingTrades[0]
	e.pending[code] = pendingTrades[1:]
	if returnCode != 0 {
		e.mu.Unlock()
		return
	}
	location := pending.Order.LocationID
	if location == "" {
		location = e.locationID
	}
	character := e.characterName
	orders := e.offers
	direction, source := "buy", "auction_buy_offer"
	if code == e.codes.AuctionSellRequest || code == e.codes.AuctionSellSpecificItemRequest {
		orders = e.requests
		direction, source = "sell", "auction_sell_request"
	}
	if cached, ok := orders[pending.Order.ID]; ok {
		cached.Amount -= pending.Quantity
		if cached.Amount <= 0 {
			delete(orders, pending.Order.ID)
		} else {
			orders[pending.Order.ID] = cached
		}
	}
	e.mu.Unlock()

	e.emitTrade(pending, direction, source, character, location, now)
}

func (e *Engine) emitTrade(pending pendingTrade, direction, source, character, fallbackLocation string, now time.Time) {
	timestamp := now.UTC().Format(time.RFC3339Nano)
	digest := sha256.Sum256([]byte(fmt.Sprintf("%s|%d|%d|%s", direction, pending.Order.ID, pending.Quantity, pending.RequestedAt.UTC().Format(time.RFC3339Nano))))
	unitPrice := pending.Order.UnitPrice / silverScale
	location := pending.Order.LocationID
	if location == "" {
		location = fallbackLocation
	}
	e.emit(Event{
		Type: "transaction", SourceEventID: "instant:" + hex.EncodeToString(digest[:16]),
		CapturedAt: timestamp, TradedAt: timestamp, PurchasedAt: timestamp,
		Direction: direction, TransactionKind: "instant",
		ItemID: pending.Order.ItemID, Quantity: pending.Quantity,
		UnitPrice: unitPrice, TotalPrice: unitPrice * pending.Quantity,
		OrderID: strconv.FormatInt(pending.Order.ID, 10), LocationID: location,
		CharacterName: character, QualityLevel: pending.Order.QualityLevel,
		EnchantmentLevel: pending.Order.EnchantmentLevel,
		Source:           source, Confidence: pending.Confidence,
	})
}

func (e *Engine) cacheQuickSellOrders(rawOrders []string) {
	var orders []MarketOrder
	for _, raw := range rawOrders {
		var order MarketOrder
		if json.Unmarshal([]byte(raw), &order) != nil || order.ID == 0 || order.ItemID == "" || order.Amount <= 0 || order.UnitPrice < 0 {
			continue
		}
		orders = append(orders, order)
	}
	sort.SliceStable(orders, func(i, j int) bool { return orders[i].UnitPrice > orders[j].UnitPrice })
	e.mu.Lock()
	defer e.mu.Unlock()
	for index := range orders {
		if orders[index].LocationID == "" {
			orders[index].LocationID = e.locationID
		}
		e.requests[orders[index].ID] = orders[index]
	}
	e.quickSellOrders = orders
}

func (e *Engine) findQuickSellQuantityLocked(params map[byte]interface{}) (int64, string) {
	var total int64
	for _, order := range e.quickSellOrders {
		total += order.Amount
	}
	if value, ok := asInt64(params[e.quantityParam]); ok && value > 0 && value <= total {
		return value, "confirmed"
	}
	var values []int64
	for key, value := range params {
		if key != 252 && key != 253 {
			collectIntegers(value, &values)
		}
	}
	var best int64
	for _, value := range values {
		if value > best && value <= total {
			best = value
		}
	}
	if best > 0 {
		return best, "inferred"
	}
	return 0, "inferred"
}

func (e *Engine) cacheMailInfos(params map[byte]interface{}) {
	ids := integerSlice(params[3])
	locations := stringSlice(params[7])
	if len(locations) == 0 {
		locations = stringSlice(params[6])
	}
	orderTypes := stringSlice(params[11])
	if len(orderTypes) == 0 {
		orderTypes = stringSlice(params[10])
	}
	received := integerSlice(params[12])
	if len(received) == 0 {
		received = integerSlice(params[11])
	}
	type metadata struct {
		id   int64
		info mailInfo
	}
	var emitted []metadata
	e.mu.Lock()
	for index, id := range ids {
		if index >= len(orderTypes) {
			break
		}
		location := ""
		if index < len(locations) {
			location = locations[index]
		}
		var receivedAt int64
		if index < len(received) {
			receivedAt = received[index]
		}
		if !isMarketOrderMailType(orderTypes[index]) {
			continue
		}
		info := mailInfo{LocationID: location, OrderType: orderTypes[index], Received: receivedAt}
		e.mailInfos[id] = info
		emitted = append(emitted, metadata{id: id, info: info})
	}
	e.mu.Unlock()
	for _, value := range emitted {
		e.emit(Event{Type: "mail_metadata", SourceEventID: "mail-meta:" + strconv.FormatInt(value.id, 10),
			CapturedAt: time.Now().UTC().Format(time.RFC3339Nano), MailID: value.id,
			MailType: value.info.OrderType, MailReceived: value.info.Received,
			LocationID: value.info.LocationID, Source: "get_mail_infos"})
	}
}

func (e *Engine) handleReadMail(params map[byte]interface{}, now time.Time) {
	mailID, idOK := asInt64(params[0])
	body, bodyOK := params[1].(string)
	if !idOK || !bodyOK {
		return
	}
	e.mu.Lock()
	info, found := e.mailInfos[mailID]
	character := e.characterName
	e.mu.Unlock()
	if !found {
		return
	}
	parts := strings.Split(body, "|")
	var quantity, unitPrice, totalPrice int64
	var itemID, confidence, direction, source, resolution string
	switch info.OrderType {
	case "MARKETPLACE_BUYORDER_FINISHED_SUMMARY":
		if len(parts) < 4 {
			return
		}
		quantity, _ = strconv.ParseInt(parts[0], 10, 64)
		itemID = parts[1]
		internalTotal, totalError := strconv.ParseInt(parts[2], 10, 64)
		totalPrice = internalTotal / silverScale
		if totalError != nil || totalPrice <= 0 {
			legacyUnit, _ := strconv.ParseInt(parts[3], 10, 64)
			unitPrice = legacyUnit / silverScale
			totalPrice = quantity * unitPrice
		}
		if quantity > 0 {
			if unitPrice == 0 {
				unitPrice = (totalPrice + quantity/2) / quantity
			}
		}
		direction, source, confidence = "buy", "buy_order_mail", "confirmed"
		resolution = "completed"
	case "MARKETPLACE_BUYORDER_EXPIRED_SUMMARY":
		if len(parts) < 4 {
			return
		}
		quantity, _ = strconv.ParseInt(parts[0], 10, 64)
		ordered, _ := strconv.ParseInt(parts[1], 10, 64)
		internalRemainingTotal, _ := strconv.ParseInt(parts[2], 10, 64)
		itemID = parts[3]
		remaining := ordered - quantity
		if remaining > 0 {
			unitPrice = (internalRemainingTotal / remaining) / silverScale
		}
		totalPrice = quantity * unitPrice
		direction, source, confidence = "buy", "buy_order_mail", "inferred"
		resolution = "partial"
	case "MARKETPLACE_SELLORDER_FINISHED_SUMMARY":
		if len(parts) < 4 {
			return
		}
		quantity, _ = strconv.ParseInt(parts[0], 10, 64)
		itemID = parts[1]
		internalTotal, totalError := strconv.ParseInt(parts[2], 10, 64)
		totalPrice = internalTotal / silverScale
		if totalError != nil || totalPrice <= 0 {
			legacyUnit, _ := strconv.ParseInt(parts[3], 10, 64)
			unitPrice = legacyUnit / silverScale
			totalPrice = quantity * unitPrice
		}
		if quantity > 0 {
			if unitPrice == 0 {
				unitPrice = (totalPrice + quantity/2) / quantity
			}
		}
		direction, source, confidence = "sell", "sell_order_mail", "confirmed"
		resolution = "completed"
	case "MARKETPLACE_SELLORDER_EXPIRED_SUMMARY":
		if len(parts) < 4 {
			return
		}
		quantity, _ = strconv.ParseInt(parts[0], 10, 64)
		internalTotal, _ := strconv.ParseInt(parts[2], 10, 64)
		itemID = parts[3]
		totalPrice = internalTotal / silverScale
		if quantity > 0 {
			unitPrice = (totalPrice + quantity/2) / quantity
		}
		direction, source, confidence = "sell", "sell_order_mail", "confirmed"
		resolution = "partial"
	case "BLACKMARKET_SELLORDER_EXPIRED_SUMMARY":
		if len(parts) < 4 {
			return
		}
		quantity, _ = strconv.ParseInt(parts[0], 10, 64)
		internalTotal, _ := strconv.ParseInt(parts[2], 10, 64)
		itemID = parts[3]
		totalPrice = internalTotal / silverScale
		if quantity > 0 {
			unitPrice = (totalPrice + quantity/2) / quantity
		}
		direction, source, confidence = "sell", "sell_order_mail", "confirmed"
		resolution = "partial"
	default:
		return
	}
	if quantity == 0 && (resolution == "partial") {
		e.emitMailResolution(mailID, info, "no_trade", body, "訂單到期但成交數量為 0", now)
		return
	}
	if quantity <= 0 || unitPrice < 0 || itemID == "" {
		e.emitMailResolution(mailID, info, "parse_error", body, "市場郵件內容無法解析", now)
		go e.warning("mail_parse_failed", "收到市場成交郵件，但內容無法解析", fmt.Sprintf("mail=%d type=%s body=%s", mailID, info.OrderType, body))
		return
	}
	if totalPrice == 0 {
		totalPrice = quantity * unitPrice
	}
	tradeTime := mailTimestamp(info.Received, now)
	timestamp := tradeTime.UTC().Format(time.RFC3339Nano)
	e.emit(Event{
		Type: "transaction", SourceEventID: "mail:" + strconv.FormatInt(mailID, 10),
		CapturedAt: timestamp, TradedAt: timestamp, PurchasedAt: timestamp,
		Direction: direction, TransactionKind: "order", ItemID: itemID,
		Quantity: quantity, UnitPrice: unitPrice, TotalPrice: totalPrice,
		OrderID: "mail:" + strconv.FormatInt(mailID, 10), LocationID: info.LocationID,
		CharacterName: character, Source: source, Confidence: confidence,
		MailID: mailID, MailType: info.OrderType, MailReceived: info.Received, MailState: resolution, RawParams: body,
	})
}

func (e *Engine) emitMailResolution(mailID int64, info mailInfo, state, body, message string, now time.Time) {
	e.emit(Event{
		Type: "mail_resolution", SourceEventID: "mail-resolution:" + strconv.FormatInt(mailID, 10),
		CapturedAt: now.UTC().Format(time.RFC3339Nano), MailID: mailID, MailType: info.OrderType,
		MailReceived: info.Received, MailState: state, LocationID: info.LocationID,
		RawParams: body, Message: message, Source: "read_mail",
	})
}

func isMarketOrderMailType(value string) bool {
	switch value {
	case "MARKETPLACE_BUYORDER_FINISHED_SUMMARY", "MARKETPLACE_BUYORDER_EXPIRED_SUMMARY",
		"MARKETPLACE_SELLORDER_FINISHED_SUMMARY", "MARKETPLACE_SELLORDER_EXPIRED_SUMMARY",
		"BLACKMARKET_SELLORDER_EXPIRED_SUMMARY":
		return true
	default:
		return false
	}
}

func mailTimestamp(value int64, fallback time.Time) time.Time {
	const dotNetUnixEpochTicks int64 = 621355968000000000
	if value > dotNetUnixEpochTicks {
		return time.Unix(0, (value-dotNetUnixEpochTicks)*100).UTC()
	}
	if value > 1_000_000_000_000 {
		return time.UnixMilli(value).UTC()
	}
	if value > 1_000_000_000 {
		return time.Unix(value, 0).UTC()
	}
	return fallback
}

func (e *Engine) cacheOrders(rawOrders []string, buyRequests bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, raw := range rawOrders {
		var order MarketOrder
		if err := json.Unmarshal([]byte(raw), &order); err != nil || order.ID == 0 || order.ItemID == "" {
			continue
		}
		auctionType := strings.ToLower(order.AuctionType)
		isRequest := strings.Contains(auctionType, "request") || strings.Contains(auctionType, "buy")
		if isRequest != buyRequests {
			continue
		}
		if order.Amount <= 0 || order.UnitPrice < 0 {
			continue
		}
		if order.LocationID == "" {
			order.LocationID = e.locationID
		}
		if buyRequests {
			e.requests[order.ID] = order
		} else {
			e.offers[order.ID] = order
		}
	}
}

func (e *Engine) findOrderLocked(params map[byte]interface{}, orders map[int64]MarketOrder) (MarketOrder, bool) {
	if value, ok := asInt64(params[e.orderIDParam]); ok {
		if order, exists := orders[value]; exists {
			return order, true
		}
	}
	values := make([]int64, 0, len(params))
	for key, value := range params {
		if key == 252 || key == 253 {
			continue
		}
		collectIntegers(value, &values)
	}
	for _, value := range values {
		if order, exists := orders[value]; exists {
			return order, true
		}
	}
	return MarketOrder{}, false
}

func (e *Engine) findQuantityLocked(params map[byte]interface{}, order MarketOrder) (int64, string) {
	if value, ok := asInt64(params[e.quantityParam]); ok && value > 0 && value <= order.Amount {
		return value, "confirmed"
	}
	var values []int64
	for key, value := range params {
		if key == e.orderIDParam || key == 252 || key == 253 {
			continue
		}
		collectIntegers(value, &values)
	}
	for _, value := range values {
		if value > 0 && value <= order.Amount && value != order.ID {
			return value, "inferred"
		}
	}
	return 1, "inferred"
}

func (e *Engine) pruneLocked(now time.Time) {
	for code, pending := range e.pending {
		valid := pending[:0]
		for _, item := range pending {
			if now.Sub(item.RequestedAt) < 30*time.Second {
				valid = append(valid, item)
			}
		}
		e.pending[code] = valid
	}
	for signature, created := range e.recentRequests {
		if now.Sub(created) >= 2*time.Second {
			delete(e.recentRequests, signature)
		}
	}
}

func (e *Engine) warning(code, message, raw string) {
	now := time.Now().UTC()
	e.mu.Lock()
	if previous := e.warningThrottles[code]; !previous.IsZero() && now.Sub(previous) < 30*time.Second {
		e.mu.Unlock()
		return
	}
	e.warningThrottles[code] = now
	e.mu.Unlock()
	digest := sha256.Sum256([]byte(code + "|" + raw + "|" + now.Truncate(30*time.Second).String()))
	e.emit(Event{
		Type: "capture_warning", SourceEventID: "warning:" + hex.EncodeToString(digest[:12]),
		CapturedAt: now.Format(time.RFC3339Nano), Code: code, Message: message, RawParams: raw,
	})
}

func resolveCode(opCode byte, params map[byte]interface{}) uint16 {
	if value, ok := asInt64(params[253]); ok && value >= 0 && value <= 65535 {
		return uint16(value)
	}
	return uint16(opCode)
}

func asInt64(value interface{}) (int64, bool) {
	switch v := value.(type) {
	case int:
		return int64(v), true
	case int8:
		return int64(v), true
	case int16:
		return int64(v), true
	case int32:
		return int64(v), true
	case int64:
		return v, true
	case uint:
		return int64(v), true
	case uint8:
		return int64(v), true
	case uint16:
		return int64(v), true
	case uint32:
		return int64(v), true
	case uint64:
		if v <= ^uint64(0)>>1 {
			return int64(v), true
		}
	case json.Number:
		n, err := v.Int64()
		return n, err == nil
	}
	return 0, false
}

func collectIntegers(value interface{}, output *[]int64) {
	if number, ok := asInt64(value); ok {
		*output = append(*output, number)
		return
	}
	switch values := value.(type) {
	case []interface{}:
		for _, item := range values {
			collectIntegers(item, output)
		}
	case []int64:
		*output = append(*output, values...)
	case []int:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []int16:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []int32:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []uint:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []uint16:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []uint32:
		for _, item := range values {
			*output = append(*output, int64(item))
		}
	case []uint64:
		for _, item := range values {
			if item <= ^uint64(0)>>1 {
				*output = append(*output, int64(item))
			}
		}
	}
}

func integerSlice(value interface{}) []int64 {
	var values []int64
	collectIntegers(value, &values)
	return values
}

func stringSlice(value interface{}) []string {
	switch values := value.(type) {
	case []string:
		return values
	case []interface{}:
		result := make([]string, 0, len(values))
		for _, item := range values {
			if text, ok := item.(string); ok {
				result = append(result, text)
			}
		}
		return result
	case string:
		return []string{values}
	default:
		return nil
	}
}

func orderStrings(params map[byte]interface{}) []string {
	for _, value := range params {
		values := stringSlice(value)
		for _, candidate := range values {
			if strings.HasPrefix(strings.TrimSpace(candidate), "{") && strings.Contains(candidate, "ItemTypeId") {
				return values
			}
		}
	}
	return nil
}

func normalizeLocation(value string) string {
	return strings.TrimSpace(strings.TrimSuffix(value, "_TUTORIAL"))
}

func formatParams(params map[byte]interface{}) string {
	keys := make([]int, 0, len(params))
	for key := range params {
		keys = append(keys, int(key))
	}
	sort.Ints(keys)
	var result strings.Builder
	for _, key := range keys {
		if result.Len() > 0 {
			result.WriteString(" ")
		}
		fmt.Fprintf(&result, "%d:%v", key, params[byte(key)])
		if result.Len() > 2000 {
			return result.String()[:2000]
		}
	}
	return result.String()
}
