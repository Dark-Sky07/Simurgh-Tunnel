package limits

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseSizeMatchesThePythonEngine(t *testing.T) {
	cases := map[string]int64{
		"10M": 10 << 20, "500k": 500 << 10, "2G": 2 << 30, "1.5M": 1572864,
		"3MiB": 3 << 20, "1T": 1 << 40, "0": 0, "": 0, "2048": 2048,
		" 20 m ": 20 << 20,
	}
	for text, want := range cases {
		if got := ParseSize(text); got != want {
			t.Errorf("ParseSize(%q) = %d, want %d", text, got, want)
		}
	}
	if got := ParseSize(4096); got != 4096 {
		t.Errorf("ParseSize(int) = %d", got)
	}
	if got := ParseSize(4096.0); got != 4096 {
		t.Errorf("ParseSize(float) = %d", got)
	}
	if got := ParseSize("nonsense"); got != 0 {
		t.Errorf("ParseSize(garbage) = %d, want 0", got)
	}
}

func TestBucketLimitsThroughput(t *testing.T) {
	bucket := NewBucket(64*1024, 0, "test")
	if !bucket.Enabled() {
		t.Fatal("a 64k bucket should be enabled")
	}
	if bucket.Burst != 64*1024 {
		t.Fatalf("burst = %d, want one second of traffic", bucket.Burst)
	}
	// the first second is free (the bucket starts full), the second one is not
	start := time.Now()
	passed := 0
	deadline := start.Add(1500 * time.Millisecond)
	for time.Now().Before(deadline) {
		allowed, delay := bucket.Allowance(4096)
		passed += allowed
		if allowed == 0 {
			if delay <= 0 {
				t.Fatal("a dry bucket must ask the caller to wait")
			}
			time.Sleep(delay)
		}
	}
	elapsed := time.Since(start).Seconds()
	rate := float64(passed) / elapsed
	if rate < 40<<10 || rate > 110<<10 {
		t.Fatalf("passed %d bytes in %.2fs = %.1f KiB/s, want about 64 KiB/s",
			passed, elapsed, rate/1024)
	}
}

func TestBucketOffIsUnlimited(t *testing.T) {
	if NewBucket(0, 0, "off").Enabled() {
		t.Fatal("a zero rate must be unlimited")
	}
	if Limiter(nil, "x") != nil || Limiter("", "x") != nil {
		t.Fatal("an empty value must produce no bucket at all")
	}
	if Limiter(map[string]any{"rate": "10M", "burst": "2M"}, "x") == nil {
		t.Fatal("an inline table must produce a bucket")
	}
	if Limiter("20M", "x").Rate != 20<<20 {
		t.Fatal("a size string must be read")
	}
}

func TestQuotaStates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "traffic.json")
	account := Open(path)
	account.Add("tcp:0.0.0.0:443/127.0.0.1:8443", 300, 200)
	if got := account.Usage("tcp:0.0.0.0:443/127.0.0.1:8443", "total"); got != 500 {
		t.Fatalf("usage = %d, want 500", got)
	}
	quota := QuotaFrom("1K", "month", "block", "m")
	if quota.Enabled() != true || quota.Limit != 1024 {
		t.Fatalf("quota = %+v", quota)
	}
	if quota.Exceeded(account, "tcp:0.0.0.0:443/127.0.0.1:8443") {
		t.Fatal("500 of 1024 must not count as exceeded")
	}
	account.Add("tcp:0.0.0.0:443/127.0.0.1:8443", 0, 600)
	state := quota.State(account, "tcp:0.0.0.0:443/127.0.0.1:8443")
	if state["exceeded"] != true {
		t.Fatalf("1100 of 1024 must be exceeded: %v", state)
	}
	if state["remaining"].(int64) != 0 {
		t.Fatalf("remaining = %v, want 0", state["remaining"])
	}
	// an inline table wins over the plain value
	table := QuotaFrom(map[string]any{"bytes": "2M", "period": "day", "action": "throttle"},
		"month", "block", "m")
	if table.Limit != 2<<20 || table.Period != "day" || table.Action != "throttle" {
		t.Fatalf("table quota = %+v", table)
	}
}

func TestLedgerSurvivesARestart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "traffic.json")
	first := Open(path)
	first.Add("mapping", 1000, 2000)
	first.Add("token:relay1", 50, 0)
	first.Save(true)

	// the file has to be the shape the Python engine reads
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read the ledger: %v", err)
	}
	var data struct {
		Version int `json:"version"`
		Keys    map[string]struct {
			Total struct {
				In  int64 `json:"in"`
				Out int64 `json:"out"`
			} `json:"total"`
			Days   map[string]any `json:"days"`
			Months map[string]any `json:"months"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		t.Fatalf("the ledger is not valid JSON: %v", err)
	}
	if data.Version != LedgerVersion {
		t.Fatalf("version = %d", data.Version)
	}
	if data.Keys["mapping"].Total.In != 1000 || data.Keys["mapping"].Total.Out != 2000 {
		t.Fatalf("totals lost: %+v", data.Keys["mapping"].Total)
	}
	if len(data.Keys["mapping"].Days) != 1 || len(data.Keys["mapping"].Months) != 1 {
		t.Fatal("the daily and monthly buckets have to be written too")
	}

	second := Open(path)
	if got := second.Usage("mapping", "total"); got != 3000 {
		t.Fatalf("after a restart usage = %d, want 3000", got)
	}
	if got := second.Usage("token:relay1", "day"); got != 50 {
		t.Fatalf("today's usage for the token = %d, want 50", got)
	}
	totals := second.Totals("")["total"].(int64)
	if totals != 3050 {
		t.Fatalf("node totals = %d, want 3050", totals)
	}
	rows := second.Rows("month")
	if len(rows) != 2 || rows[0]["key"] != "mapping" {
		t.Fatalf("rows = %v", rows)
	}
}

func TestPruneDropsOldDays(t *testing.T) {
	account := Open(filepath.Join(t.TempDir(), "traffic.json"))
	account.Add("k", 10, 10)
	account.mu.Lock()
	account.keys["k"].Days["2000-01-01"] = pair{In: 5, Out: 5}
	account.mu.Unlock()
	account.Prune(KeepDays)
	account.mu.Lock()
	_, old := account.keys["k"].Days["2000-01-01"]
	account.mu.Unlock()
	if old {
		t.Fatal("a bucket from 2000 should have been pruned")
	}
	if account.Usage("k", "total") != 20 {
		t.Fatal("pruning must not touch the running total")
	}
}
