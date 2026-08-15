package capture

import (
	"testing"
	"time"
)

func TestSuccessfulPurchaseIsEmitted(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Date(2026, 8, 14, 10, 0, 0, 0, time.UTC)
	engine.HandleRequest(17, map[byte]interface{}{0: "3005"}, now)
	engine.HandleResponse(81, 0, map[byte]interface{}{0: []string{
		`{"Id":991122,"ItemTypeId":"T4_BAG","LocationId":null,"QualityLevel":2,"EnchantmentLevel":0,"UnitPriceSilver":12500000,"Amount":20,"AuctionType":"offer"}`,
	}}, now)
	engine.HandleRequest(83, map[byte]interface{}{0: int64(991122), 1: int32(3)}, now.Add(time.Second))
	engine.HandleResponse(83, 0, map[byte]interface{}{}, now.Add(2*time.Second))

	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
	event := events[0]
	if event.ItemID != "T4_BAG" || event.Quantity != 3 || event.UnitPrice != 1250 || event.TotalPrice != 3750 {
		t.Fatalf("unexpected purchase: %#v", event)
	}
	if event.LocationID != "3005" || event.Confidence != "confirmed" {
		t.Fatalf("unexpected metadata: %#v", event)
	}
}

func TestFailedResponseDoesNotEmit(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(81, 0, map[byte]interface{}{0: []string{
		`{"Id":7,"ItemTypeId":"T5_CAPE","UnitPriceSilver":9000000,"Amount":1,"AuctionType":"offer"}`,
	}}, now)
	engine.HandleRequest(83, map[byte]interface{}{0: int64(7), 1: int32(1)}, now)
	engine.HandleResponse(83, 5, map[byte]interface{}{}, now)
	if len(events) != 0 {
		t.Fatalf("failed purchase emitted %#v", events)
	}
}

func TestDuplicateCapturedRequestIsIgnored(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(81, 0, map[byte]interface{}{0: []string{
		`{"Id":8,"ItemTypeId":"T6_MAIN_SWORD","UnitPriceSilver":30000000,"Amount":2,"AuctionType":"offer"}`,
	}}, now)
	request := map[byte]interface{}{0: int64(8), 1: int32(1)}
	engine.HandleRequest(83, request, now)
	engine.HandleRequest(83, request, now.Add(100*time.Millisecond))
	engine.HandleResponse(83, 0, map[byte]interface{}{}, now.Add(time.Second))
	engine.HandleResponse(83, 0, map[byte]interface{}{}, now.Add(time.Second))
	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
}

func TestInferredQuantityUsesStableParameterOrder(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(82, 0, map[byte]interface{}{0: []string{
		`{"Id":91,"ItemTypeId":"T4_BAG","UnitPriceSilver":250000,"Amount":20,"AuctionType":"request"}`,
	}}, now)
	// Field 1 is absent.  The fallback must consistently prefer field 2 (3),
	// rather than an arbitrary map entry such as field 4 (9).
	engine.HandleRequest(88, map[byte]interface{}{0: int64(91), 4: int32(9), 2: int32(3)}, now)
	engine.HandleResponse(88, 0, map[byte]interface{}{}, now.Add(time.Second))
	if len(events) != 1 || events[0].Quantity != 3 || events[0].Confidence != "inferred" {
		t.Fatalf("unexpected inferred event: %#v", events)
	}
}

func TestSellSpecificItemUsesItsProtocolQuantityField(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(82, 0, map[byte]interface{}{0: []string{
		`{"Id":3183759429,"ItemTypeId":"T2_FURNITUREITEM_TROPHY_GENERAL","UnitPriceSilver":20000,"Amount":2120,"AuctionType":"request"}`,
	}}, now)
	// Operation 315: field 3 is unrelated metadata, while field 4 is the
	// selected quantity.  This represents selling one trophy, not three.
	engine.HandleRequest(228, map[byte]interface{}{
		0: int32(6), 1: int64(3183759429), 2: int32(9684), 3: int32(3), 4: int32(1), 253: int64(315),
	}, now)
	engine.HandleResponse(228, 0, map[byte]interface{}{253: int64(315)}, now.Add(time.Second))
	if len(events) != 1 || events[0].Quantity != 1 || events[0].Confidence != "confirmed" {
		t.Fatalf("unexpected sell-specific event: %#v", events)
	}
}

func TestSuccessfulInstantSaleIsEmitted(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Date(2026, 8, 14, 11, 0, 0, 0, time.UTC)
	engine.HandleRequest(17, map[byte]interface{}{0: "1002"}, now)
	engine.HandleResponse(82, 0, map[byte]interface{}{0: []string{
		`{"Id":7788,"ItemTypeId":"T1_HIDE","UnitPriceSilver":100000,"Amount":10,"AuctionType":"request"}`,
	}}, now)
	engine.HandleRequest(88, map[byte]interface{}{0: int64(7788), 1: int32(2)}, now.Add(time.Second))
	engine.HandleResponse(88, 0, map[byte]interface{}{}, now.Add(2*time.Second))

	if len(events) != 1 {
		t.Fatalf("got %d events, want 1", len(events))
	}
	event := events[0]
	if event.Direction != "sell" || event.TransactionKind != "instant" || event.UnitPrice != 10 || event.TotalPrice != 20 {
		t.Fatalf("unexpected sale: %#v", event)
	}
}

func TestSuccessfulQuickSaleCanConsumeMultipleBuyOrders(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Date(2026, 8, 14, 11, 30, 0, 0, time.UTC)
	engine.HandleRequest(17, map[byte]interface{}{0: "1002"}, now)
	engine.HandleResponse(228, 0, map[byte]interface{}{
		253: int64(484),
		5: []string{
			`{"Id":9001,"ItemTypeId":"T1_HIDE","UnitPriceSilver":120000,"Amount":2,"AuctionType":"request"}`,
			`{"Id":9002,"ItemTypeId":"T1_HIDE","UnitPriceSilver":100000,"Amount":5,"AuctionType":"request"}`,
		},
	}, now)
	engine.HandleRequest(229, map[byte]interface{}{253: int64(485), 1: int32(3)}, now.Add(time.Second))
	engine.HandleResponse(229, 0, map[byte]interface{}{253: int64(485)}, now.Add(2*time.Second))
	if len(events) != 2 {
		t.Fatalf("got %d events, want 2", len(events))
	}
	if events[0].Direction != "sell" || events[0].Quantity != 2 || events[0].UnitPrice != 12 || events[1].Quantity != 1 || events[1].UnitPrice != 10 {
		t.Fatalf("unexpected quick sale: %#v", events)
	}
}

func TestFinishedSellOrderMailIsEmitted(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(174, 0, map[byte]interface{}{
		3:  []int64{5502},
		6:  []string{"1002"},
		10: []string{"MARKETPLACE_SELLORDER_FINISHED_SUMMARY"},
	}, now)
	engine.HandleResponse(176, 0, map[byte]interface{}{
		0: int64(5502), 1: "4|T1_HIDE|unused|120000",
	}, now.Add(time.Second))
	if len(events) != 2 || events[0].Type != "mail_metadata" {
		t.Fatalf("got %#v, want metadata followed by transaction", events)
	}
	trade := events[1]
	if trade.Direction != "sell" || trade.TransactionKind != "order" || trade.UnitPrice != 12 || trade.TotalPrice != 48 || trade.MailID != 5502 {
		t.Fatalf("unexpected sell-order mail: %#v", trade)
	}
}

func TestFinishedBuyOrderMailIsEmittedOnceByDatabaseEventID(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(174, 0, map[byte]interface{}{
		3:  []int64{4401},
		6:  []string{"3008"},
		10: []string{"MARKETPLACE_BUYORDER_FINISHED_SUMMARY"},
	}, now)
	engine.HandleResponse(176, 0, map[byte]interface{}{
		0: int64(4401), 1: "5|T6_ORE|500000000|100000000",
	}, now.Add(time.Second))
	if len(events) != 2 || events[0].Type != "mail_metadata" {
		t.Fatalf("got %#v, want metadata followed by transaction", events)
	}
	trade := events[1]
	if trade.Quantity != 5 || trade.UnitPrice != 10000 || trade.TotalPrice != 50000 || trade.MailID != 4401 {
		t.Fatalf("unexpected buy-order mail: %#v", trade)
	}
}

func TestExpiredSellOrderWithNoSalesIsResolvedWithoutTransaction(t *testing.T) {
	var events []Event
	engine := NewEngine(DefaultOperationCodes(), 0, 1, func(event Event) { events = append(events, event) })
	now := time.Now()
	engine.HandleResponse(174, 0, map[byte]interface{}{
		3: []int64{6601}, 7: []string{"1002"}, 11: []string{"MARKETPLACE_SELLORDER_EXPIRED_SUMMARY"},
	}, now)
	engine.HandleResponse(176, 0, map[byte]interface{}{
		0: int64(6601), 1: "0|39|0|T7_JOURNAL_HUNTER_FULL|",
	}, now.Add(time.Second))
	if len(events) != 2 || events[0].Type != "mail_metadata" || events[1].Type != "mail_resolution" {
		t.Fatalf("unexpected events: %#v", events)
	}
	if events[1].MailState != "no_trade" {
		t.Fatalf("got state %q, want no_trade", events[1].MailState)
	}
}
