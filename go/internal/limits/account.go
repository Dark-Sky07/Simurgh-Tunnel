package limits

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// The ledger file is the same traffic.json the Python engine writes, so an
// operator can switch engines and keep the month's numbers.
const (
	LedgerVersion = 1
	SaveInterval  = 15 * time.Second
	KeepDays      = 92
)

type pair struct {
	In  int64 `json:"in"`
	Out int64 `json:"out"`
}

type entry struct {
	Total  pair            `json:"total"`
	Days   map[string]pair `json:"days"`
	Months map[string]pair `json:"months"`
}

type ledger struct {
	Version int              `json:"version"`
	Updated float64          `json:"updated"`
	Keys    map[string]entry `json:"keys"`
}

// Account keeps per-key counters (a mapping, a token, the tunnel itself) and
// flushes them to disk every few seconds and on the way out.
type Account struct {
	Path string

	mu      sync.Mutex
	keys    map[string]*entry
	started time.Time
	dirty   bool
	stop    chan struct{}
	once    sync.Once
	wg      sync.WaitGroup
}

// Open reads a ledger (missing or unreadable files start empty).
func Open(path string) *Account {
	a := &Account{Path: path, keys: map[string]*entry{}, started: time.Now()}
	if path != "" {
		a.load()
	}
	return a
}

func (a *Account) load() {
	raw, err := os.ReadFile(a.Path)
	if err != nil {
		return
	}
	var data ledger
	if err := json.Unmarshal(raw, &data); err != nil || data.Version != LedgerVersion {
		return
	}
	for key, e := range data.Keys {
		copied := entry{Total: e.Total, Days: map[string]pair{}, Months: map[string]pair{}}
		for day, p := range e.Days {
			copied.Days[day] = p
		}
		for month, p := range e.Months {
			copied.Months[month] = p
		}
		a.keys[key] = &copied
	}
}

// Start begins the background flusher.
func (a *Account) Start() {
	if a.Path == "" {
		return
	}
	a.stop = make(chan struct{})
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		ticker := time.NewTicker(SaveInterval)
		defer ticker.Stop()
		for {
			select {
			case <-a.stop:
				return
			case <-ticker.C:
				a.Save(false)
			}
		}
	}()
}

// Stop flushes and stops the flusher.
func (a *Account) Stop() {
	a.once.Do(func() {
		if a.stop != nil {
			close(a.stop)
		}
	})
	a.wg.Wait()
	a.Save(true)
}

// Add counts traffic for one key.
func (a *Account) Add(key string, inBytes, outBytes int64) {
	if key == "" || (inBytes <= 0 && outBytes <= 0) {
		return
	}
	if inBytes < 0 {
		inBytes = 0
	}
	if outBytes < 0 {
		outBytes = 0
	}
	now := time.Now()
	day := now.Format("2006-01-02")
	month := now.Format("2006-01")
	a.mu.Lock()
	e := a.keys[key]
	if e == nil {
		e = &entry{Days: map[string]pair{}, Months: map[string]pair{}}
		a.keys[key] = e
	}
	e.Total.In += inBytes
	e.Total.Out += outBytes
	d, ok := e.Days[day]
	if !ok {
		e.Days[day] = pair{In: inBytes, Out: outBytes}
	} else {
		e.Days[day] = pair{In: d.In + inBytes, Out: d.Out + outBytes}
	}
	m, ok := e.Months[month]
	if !ok {
		e.Months[month] = pair{In: inBytes, Out: outBytes}
	} else {
		e.Months[month] = pair{In: m.In + inBytes, Out: m.Out + outBytes}
	}
	a.dirty = true
	a.mu.Unlock()
}

// Usage is the counted bytes of one key over a period: "day", "month" or
// anything else for the running total.
func (a *Account) Usage(key, period string) int64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	e := a.keys[key]
	if e == nil {
		return 0
	}
	now := time.Now()
	switch period {
	case "day":
		p := e.Days[now.Format("2006-01-02")]
		return p.In + p.Out
	case "month":
		p := e.Months[now.Format("2006-01")]
		return p.In + p.Out
	}
	return e.Total.In + e.Total.Out
}

// Totals aggregates one key, or every key when key is empty.
func (a *Account) Totals(key string) map[string]any {
	if key != "" {
		a.mu.Lock()
		e := a.keys[key]
		var total, in, out int64
		if e != nil {
			total = e.Total.In + e.Total.Out
			in, out = e.Total.In, e.Total.Out
		}
		a.mu.Unlock()
		return map[string]any{"total": total, "today": a.Usage(key, "day"),
			"month": a.Usage(key, "month"), "in": in, "out": out}
	}
	out := map[string]any{"total": int64(0), "today": int64(0), "month": int64(0),
		"in": int64(0), "out": int64(0)}
	for _, name := range a.Keys() {
		row := a.Totals(name)
		for _, field := range []string{"total", "today", "month", "in", "out"} {
			out[field] = out[field].(int64) + row[field].(int64)
		}
	}
	return out
}

// Keys lists the counted keys.
func (a *Account) Keys() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]string, 0, len(a.keys))
	for key := range a.keys {
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}

// Rows is what the panel's traffic table is built from.
func (a *Account) Rows(period string) []map[string]any {
	if period == "" {
		period = "month"
	}
	out := make([]map[string]any, 0)
	for _, key := range a.Keys() {
		a.mu.Lock()
		e := a.keys[key]
		in, outBytes := e.Total.In, e.Total.Out
		a.mu.Unlock()
		out = append(out, map[string]any{
			"key": key, "in": in, "out": outBytes, "total": in + outBytes,
			"today": a.Usage(key, "day"), "month": a.Usage(key, "month"),
			"period_used": a.Usage(key, period),
		})
	}
	return out
}

// Snapshot is the JSON shape `simurgh status` and the panel read.
func (a *Account) Snapshot() map[string]any {
	keys := map[string]any{}
	for _, key := range a.Keys() {
		keys[key] = map[string]any{
			"total": a.Usage(key, "total"), "today": a.Usage(key, "day"),
			"month": a.Usage(key, "month"),
		}
	}
	return map[string]any{
		"keys": keys, "totals": a.Totals(""),
		"uptime": time.Since(a.started).Seconds(),
	}
}

// Prune drops daily buckets older than keepDays.
func (a *Account) Prune(keepDays int) {
	cutoff := time.Now().AddDate(0, 0, -keepDays).Format("2006-01-02")
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, e := range a.keys {
		for day := range e.Days {
			if day < cutoff {
				delete(e.Days, day)
			}
		}
	}
	a.dirty = true
}

// Save writes the ledger atomically when something changed (or when forced).
func (a *Account) Save(force bool) {
	if a.Path == "" {
		return
	}
	a.mu.Lock()
	if !a.dirty && !force {
		a.mu.Unlock()
		return
	}
	data := ledger{Version: LedgerVersion, Updated: float64(time.Now().UnixNano()) / 1e9,
		Keys: map[string]entry{}}
	for key, e := range a.keys {
		data.Keys[key] = *e
	}
	a.dirty = false
	a.mu.Unlock()

	raw, err := json.Marshal(data)
	if err != nil {
		return
	}
	if dir := filepath.Dir(a.Path); dir != "" && dir != "." {
		_ = os.MkdirAll(dir, 0o755)
	}
	tmp := a.Path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, a.Path)
}

// ------------------------------------------------------------------- quotas

// Quota is `quota = "200G"` plus a period and what to do when it runs out.
type Quota struct {
	Limit  int64
	Period string // day | month | total
	Action string // block | throttle | warn
	Name   string
}

// NewQuota normalises the period and action the way the Python engine does.
func NewQuota(limit int64, period, action, name string) Quota {
	if period != "day" && period != "month" && period != "total" {
		period = "month"
	}
	if action != "block" && action != "throttle" && action != "warn" {
		action = "block"
	}
	if limit < 0 {
		limit = 0
	}
	return Quota{Limit: limit, Period: period, Action: action, Name: name}
}

// Enabled reports whether a quota is configured.
func (q Quota) Enabled() bool { return q.Limit > 0 }

// Exceeded reports whether the ledger already shows more than the limit.
func (q Quota) Exceeded(a *Account, key string) bool {
	return q.Enabled() && a.Usage(key, q.Period) >= q.Limit
}

// State is the shape the panel shows for a quota.
func (q Quota) State(a *Account, key string) map[string]any {
	used := int64(0)
	if a != nil {
		used = a.Usage(key, q.Period)
	}
	remaining := q.Limit - used
	if remaining < 0 {
		remaining = 0
	}
	percent := 0.0
	if q.Limit > 0 {
		percent = float64(int64(1000*float64(used)/float64(q.Limit)+0.5)) / 10
	}
	return map[string]any{
		"limit": q.Limit, "period": q.Period, "action": q.Action, "used": used,
		"remaining": remaining, "exceeded": q.Enabled() && used >= q.Limit,
		"percent": percent,
	}
}

// Describe is the one-line form used in logs and the panel.
func (q Quota) Describe() string {
	if !q.Enabled() {
		return "unlimited"
	}
	return Human(float64(q.Limit)) + "/" + q.Period + " (" + q.Action + ")"
}

// QuotaFrom builds a quota from a config value (size string, number or table).
func QuotaFrom(value any, period, action, name string) Quota {
	if value == nil || value == "" || value == false {
		return NewQuota(0, period, action, name)
	}
	if table, ok := value.(map[string]any); ok {
		if p, ok := table["period"].(string); ok && p != "" {
			period = p
		}
		if a, ok := table["action"].(string); ok && a != "" {
			action = a
		}
		value = table["bytes"]
		if value == nil {
			value = table["limit"]
		}
		if value == nil {
			value = table["quota"]
		}
	}
	return NewQuota(ParseSize(value), period, action, name)
}
