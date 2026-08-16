package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/ao-data/albiondata-client/client/photon"
	"github.com/google/gopacket"
	"github.com/google/gopacket/layers"
	"github.com/google/gopacket/pcap"
	tracker "github.com/local/albion-purchase-tracker/internal/capture"
)

type config struct {
	apiURL            string
	devices           string
	port              int
	listDevices       bool
	offlinePCAP       string
	spoolPath         string
	logPath           string
	clientID          string
	orderIDParam      int
	quantityParam     int
	joinOp            int
	gameServerOp      int
	offersOp          int
	requestsOp        int
	buyOp             int
	sellOp            int
	sellSpecificOp    int
	quickSellQueryOp  int
	quickSellActionOp int
	getMailInfosOp    int
	readMailOp        int
}

type queuedEvent struct {
	payload interface{}
	durable bool
}

type sender struct {
	endpoint string
	spool    string
	client   *http.Client
	mu       sync.Mutex
}

type statusEvent struct {
	Type          string `json:"type"`
	ClientID      string `json:"client_id"`
	CapturedAt    string `json:"captured_at"`
	State         string `json:"state"`
	LastPacketAt  string `json:"last_packet_at,omitempty"`
	PacketsSeen   uint64 `json:"packets_seen"`
	LocationID    string `json:"location_id,omitempty"`
	CharacterName string `json:"character_name,omitempty"`
	Message       string `json:"message,omitempty"`
	Version       string `json:"version"`
}

func main() {
	cfg := parseFlags()
	if cfg.logPath != "" {
		if err := os.MkdirAll(filepath.Dir(cfg.logPath), 0755); err != nil {
			log.Fatal(err)
		}
		file, err := os.OpenFile(cfg.logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
		if err != nil {
			log.Fatal(err)
		}
		defer file.Close()
		log.SetOutput(file)
	}
	if cfg.listDevices {
		if err := printDevices(); err != nil {
			log.Fatal(err)
		}
		return
	}
	if cfg.orderIDParam < 0 || cfg.orderIDParam > 255 || cfg.quantityParam < 0 || cfg.quantityParam > 255 {
		log.Fatal("order-id-param 與 quantity-param 必須介於 0 到 255")
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	events := make(chan queuedEvent, 1000)
	sink := &sender{
		endpoint: strings.TrimRight(cfg.apiURL, "/") + "/api/events",
		spool:    cfg.spoolPath,
		client:   &http.Client{Timeout: 4 * time.Second},
	}
	go sink.run(ctx, events)

	codes := tracker.OperationCodes{
		Join: uint16(cfg.joinOp), GetGameServer: uint16(cfg.gameServerOp),
		AuctionGetOffers: uint16(cfg.offersOp), AuctionGetRequests: uint16(cfg.requestsOp),
		AuctionBuyOffer: uint16(cfg.buyOp), AuctionSellRequest: uint16(cfg.sellOp),
		AuctionSellSpecificItemRequest: uint16(cfg.sellSpecificOp),
		QuickSellQuery:                 uint16(cfg.quickSellQueryOp), QuickSellAction: uint16(cfg.quickSellActionOp),
		GetMailInfos: uint16(cfg.getMailInfosOp), ReadMail: uint16(cfg.readMailOp),
	}
	engine := tracker.NewEngine(codes, byte(cfg.orderIDParam), byte(cfg.quantityParam), func(event tracker.Event) {
		events <- queuedEvent{payload: event, durable: true}
		if event.Type == "transaction" || event.Type == "purchase" || event.Type == "sale" {
			action := "購入"
			if event.Direction == "sell" {
				action = "出售"
			}
			log.Printf("已捕捉%s：%s × %d，單價 %d，總額 %d", action, event.ItemID, event.Quantity, event.UnitPrice, event.TotalPrice)
		} else {
			log.Printf("捕捉警告：%s", event.Message)
		}
	})

	go heartbeat(ctx, cfg.clientID, engine, events)
	if cfg.offlinePCAP != "" {
		if err := captureOffline(ctx, cfg.offlinePCAP, engine); err != nil {
			log.Fatal(err)
		}
		return
	}

	handles, err := captureOnline(ctx, cfg.devices, cfg.port, engine, events)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("Albion 封包捕捉器 v%s 已啟動（port %d，%d 個介面）", tracker.Version, cfg.port, len(handles))
	log.Printf("事件 API：%s", sink.endpoint)
	<-ctx.Done()
	for _, handle := range handles {
		handle.Close()
	}
	log.Print("捕捉器已停止")
}

func parseFlags() config {
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "albion-capture"
	}
	var cfg config
	flag.StringVar(&cfg.apiURL, "api", "http://127.0.0.1:8765", "本機 tracker API URL")
	flag.StringVar(&cfg.devices, "devices", "", "要監聽的介面名稱，以逗號分隔；空白代表全部")
	flag.IntVar(&cfg.port, "port", 5056, "Albion Photon port")
	flag.BoolVar(&cfg.listDevices, "list-devices", false, "列出可用網路介面後結束")
	flag.StringVar(&cfg.offlinePCAP, "pcap", "", "解析離線 .pcap，而非即時監聽")
	flag.StringVar(&cfg.spoolPath, "spool", filepath.Join("data", "capture-spool.jsonl"), "API 離線時的 JSONL 佇列")
	flag.StringVar(&cfg.logPath, "log", "", "將捕捉器日誌附加寫入指定檔案")
	flag.StringVar(&cfg.clientID, "client-id", hostname, "顯示在統計頁面的捕捉器名稱")
	flag.IntVar(&cfg.orderIDParam, "order-id-param", 0, "AuctionBuyOffer 的訂單 ID 參數索引")
	flag.IntVar(&cfg.quantityParam, "quantity-param", 1, "AuctionBuyOffer 的數量參數索引")
	flag.IntVar(&cfg.joinOp, "join-op", 2, "Join 操作碼")
	flag.IntVar(&cfg.gameServerOp, "game-server-op", 17, "GetGameServerByCluster 操作碼")
	flag.IntVar(&cfg.offersOp, "offers-op", 81, "AuctionGetOffers 操作碼")
	flag.IntVar(&cfg.requestsOp, "requests-op", 82, "AuctionGetRequests 操作碼")
	flag.IntVar(&cfg.buyOp, "buy-op", 83, "AuctionBuyOffer 操作碼")
	flag.IntVar(&cfg.sellOp, "sell-op", 88, "AuctionSellRequest 操作碼")
	flag.IntVar(&cfg.sellSpecificOp, "sell-specific-op", 315, "AuctionSellSpecificItemRequest 操作碼")
	flag.IntVar(&cfg.quickSellQueryOp, "quick-sell-query-op", 484, "QuickSellAuctionQueryAction 操作碼")
	flag.IntVar(&cfg.quickSellActionOp, "quick-sell-action-op", 485, "QuickSellAuctionSellAction 操作碼")
	flag.IntVar(&cfg.getMailInfosOp, "get-mail-infos-op", 174, "GetMailInfos 操作碼")
	flag.IntVar(&cfg.readMailOp, "read-mail-op", 176, "ReadMail 操作碼")
	flag.Parse()
	return cfg
}

func printDevices() error {
	devices, err := pcap.FindAllDevs()
	if err != nil {
		return err
	}
	for _, device := range devices {
		fmt.Printf("%s\n  %s\n", device.Name, device.Description)
		for _, address := range device.Addresses {
			fmt.Printf("  %s\n", address.IP)
		}
	}
	return nil
}

func chooseDevices(selected string) ([]pcap.Interface, error) {
	all, err := pcap.FindAllDevs()
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(selected) == "" {
		var usable []pcap.Interface
		for _, device := range all {
			if len(device.Addresses) > 0 {
				usable = append(usable, device)
			}
		}
		return usable, nil
	}
	wanted := make(map[string]bool)
	for _, name := range strings.Split(selected, ",") {
		wanted[strings.TrimSpace(name)] = true
	}
	var result []pcap.Interface
	for _, device := range all {
		if wanted[device.Name] {
			result = append(result, device)
			delete(wanted, device.Name)
		}
	}
	if len(wanted) > 0 {
		missing := make([]string, 0, len(wanted))
		for name := range wanted {
			missing = append(missing, name)
		}
		return nil, fmt.Errorf("找不到網路介面：%s", strings.Join(missing, ", "))
	}
	return result, nil
}

func captureOnline(ctx context.Context, selected string, port int, engine *tracker.Engine, events chan<- queuedEvent) ([]*pcap.Handle, error) {
	devices, err := chooseDevices(selected)
	if err != nil {
		return nil, err
	}
	var handles []*pcap.Handle
	for _, device := range devices {
		handle, openErr := pcap.OpenLive(device.Name, 65535, false, pcap.BlockForever)
		if openErr != nil {
			log.Printf("略過介面 %s：%v", device.Name, openErr)
			continue
		}
		if filterErr := handle.SetBPFFilter(fmt.Sprintf("tcp port %d or udp port %d", port, port)); filterErr != nil {
			handle.Close()
			log.Printf("介面 %s 無法設定 filter：%v", device.Name, filterErr)
			continue
		}
		handles = append(handles, handle)
		go readPackets(ctx, device.Name, handle, engine)
	}
	if len(handles) == 0 {
		message := "無法開啟任何網路介面；Windows 請安裝 Npcap 並以系統管理員執行，macOS/Linux 請用 sudo"
		events <- queuedEvent{payload: statusEvent{
			Type: "status", ClientID: "capture-error", CapturedAt: timestamp(time.Now()),
			State: "error", Message: message, Version: tracker.Version,
		}}
		return nil, errors.New(message)
	}
	return handles, nil
}

func captureOffline(ctx context.Context, path string, engine *tracker.Engine) error {
	handle, err := pcap.OpenOffline(path)
	if err != nil {
		return err
	}
	defer handle.Close()
	readPackets(ctx, path, handle, engine)
	return nil
}

func readPackets(ctx context.Context, name string, handle *pcap.Handle, engine *tracker.Engine) {
	parser := photon.NewPhotonParser(
		func(code byte, params map[byte]interface{}) { engine.HandleRequest(code, params, time.Now()) },
		func(code byte, returnCode int16, _ string, params map[byte]interface{}) {
			engine.HandleResponse(code, returnCode, params, time.Now())
		},
		nil,
	)
	parser.OnEncrypted = func() { engine.Encrypted(time.Now()) }
	source := gopacket.NewPacketSource(handle, handle.LinkType())
	packets := source.Packets()
	for {
		select {
		case <-ctx.Done():
			return
		case packet, ok := <-packets:
			if !ok {
				return
			}
			payload := packetPayload(packet)
			if len(payload) == 0 {
				continue
			}
			now := time.Now()
			engine.PacketSeen(now)
			parser.ReceivePacket(payload)
		}
	}
}

func packetPayload(packet gopacket.Packet) []byte {
	if layer := packet.Layer(layers.LayerTypeUDP); layer != nil {
		return layer.(*layers.UDP).Payload
	}
	if layer := packet.Layer(layers.LayerTypeTCP); layer != nil {
		return layer.(*layers.TCP).Payload
	}
	return nil
}

func heartbeat(ctx context.Context, clientID string, engine *tracker.Engine, events chan<- queuedEvent) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		now := time.Now()
		snapshot := engine.Snapshot()
		state, message := "waiting", "捕捉器在線，等待 Albion 遊戲流量"
		if !snapshot.LastPacketAt.IsZero() && now.Sub(snapshot.LastPacketAt) < 15*time.Second {
			state, message = "connected", "已收到 Albion 遊戲流量"
		}
		if !snapshot.LastEncrypted.IsZero() && now.Sub(snapshot.LastEncrypted) < 60*time.Second {
			state, message = "encrypted", "偵測到近期加密封包，市場解析可能暫時失效"
		}
		event := statusEvent{
			Type: "status", ClientID: clientID, CapturedAt: timestamp(now), State: state,
			PacketsSeen: snapshot.PacketsSeen, LocationID: snapshot.LocationID,
			CharacterName: snapshot.CharacterName, Message: message, Version: tracker.Version,
		}
		if !snapshot.LastPacketAt.IsZero() {
			event.LastPacketAt = timestamp(snapshot.LastPacketAt)
		}
		select {
		case events <- queuedEvent{payload: event}:
		case <-ctx.Done():
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			return
		}
	}
}

func (s *sender) run(ctx context.Context, events <-chan queuedEvent) {
	_ = s.flushSpool(ctx)
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case event := <-events:
			if err := s.post(ctx, event.payload); err != nil && event.durable {
				if spoolErr := s.appendSpool(event.payload); spoolErr != nil {
					log.Printf("事件 API 與離線佇列皆寫入失敗：%v / %v", err, spoolErr)
				}
			}
		case <-ticker.C:
			_ = s.flushSpool(ctx)
		case <-ctx.Done():
			return
		}
	}
}

func (s *sender) post(ctx context.Context, payload interface{}) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, s.endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := s.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("API 回應 %s", response.Status)
	}
	return nil
}

func (s *sender) appendSpool(payload interface{}) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(filepath.Dir(s.spool), 0755); err != nil {
		return err
	}
	file, err := os.OpenFile(s.spool, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer file.Close()
	return json.NewEncoder(file).Encode(payload)
}

func (s *sender) flushSpool(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	file, err := os.Open(s.spool)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var remaining [][]byte
	scanner := bufio.NewScanner(file)
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 2*1024*1024)
	for scanner.Scan() {
		line := append([]byte(nil), scanner.Bytes()...)
		var payload interface{}
		if json.Unmarshal(line, &payload) != nil || s.post(ctx, payload) != nil {
			remaining = append(remaining, line)
		}
	}
	closeErr := file.Close()
	if err := scanner.Err(); err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if len(remaining) == 0 {
		return os.Remove(s.spool)
	}
	temporary := s.spool + ".tmp"
	out, err := os.Create(temporary)
	if err != nil {
		return err
	}
	for _, line := range remaining {
		if _, err = out.Write(append(line, '\n')); err != nil {
			_ = out.Close()
			return err
		}
	}
	if err = out.Close(); err != nil {
		return err
	}
	return os.Rename(temporary, s.spool)
}

func timestamp(value time.Time) string {
	return value.UTC().Format(time.RFC3339Nano)
}

func init() {
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
}
