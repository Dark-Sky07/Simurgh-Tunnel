// Package limits is the Go engine's copy of simurgh/ratelimit.py and
// simurgh/accounting.py: one byte-bucket per mapping (frp's bandwidthLimit),
// plus a traffic ledger whose file layout is the same traffic.json the Python
// engine writes, so the two engines can be swapped without losing history.
package limits

import (
	"strconv"
	"strings"
	"sync"
	"time"
)

// MaxStall is the longest a data path will sleep in one go; keeping it small
// is what makes a limited link feel slow instead of dead.
const MaxStall = 250 * time.Millisecond

// Bucket is a token bucket over bytes, safe to share between connections.
// It deliberately mirrors the arithmetic of simurgh.ratelimit.RateLimiter so a
// shape of traffic behaves the same on either engine.
type Bucket struct {
	Rate  int64 // bytes per second (0 = unlimited)
	Burst int64
	Name  string

	mu      sync.Mutex
	tokens  float64
	last    time.Time
	waited  float64
	takenIn int64 // bytes passed through this bucket (for the panel)
}

// NewBucket builds a bucket.  With no explicit burst it holds one second of
// traffic, which is what keeps a short idle gap from punishing the next burst.
func NewBucket(rate, burst int64, name string) *Bucket {
	b := &Bucket{Rate: rate, Name: name}
	if rate > 0 {
		if burst <= 0 {
			burst = rate
		}
		if burst < 1024 {
			burst = 1024
		}
		b.Burst = burst
		b.tokens = float64(burst)
	}
	b.last = time.Now()
	return b
}

// Enabled reports whether the bucket limits anything at all.
func (b *Bucket) Enabled() bool { return b != nil && b.Rate > 0 }

// refill credits the bucket for the time that passed.
func (b *Bucket) refill(now time.Time) {
	dt := now.Sub(b.last).Seconds()
	if dt <= 0 {
		return
	}
	b.last = now
	if b.Rate > 0 {
		b.tokens += dt * float64(b.Rate)
		if b.tokens > float64(b.Burst) {
			b.tokens = float64(b.Burst)
		}
	}
}

// Allow consumes n bytes when the bucket has them; it never blocks.
func (b *Bucket) Allow(n int) bool {
	if !b.Enabled() || n <= 0 {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.refill(time.Now())
	if b.tokens >= float64(n) {
		b.tokens -= float64(n)
		b.takenIn += int64(n)
		return true
	}
	return false
}

// Allowance is the data-path call: how many bytes may pass right now, and how
// long the caller should wait before asking for the rest.  Parking a whole
// frame instead would never make progress for a frame bigger than the burst.
func (b *Bucket) Allowance(n int) (int, time.Duration) {
	if !b.Enabled() || n <= 0 {
		return n, 0
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.refill(time.Now())
	if b.tokens >= float64(n) {
		b.tokens -= float64(n)
		b.takenIn += int64(n)
		return n, 0
	}
	allowed := int(b.tokens)
	if allowed < 0 {
		allowed = 0
	}
	if allowed > n {
		allowed = n
	}
	b.tokens -= float64(allowed)
	b.takenIn += int64(allowed)
	missing := n - allowed
	delay := time.Duration(float64(missing) / float64(b.Rate) * float64(time.Second))
	if delay > MaxStall {
		delay = MaxStall
	}
	if delay < 0 {
		delay = 0
	}
	b.waited += delay.Seconds()
	return allowed, delay
}

// Wait sleeps until n bytes may pass, for callers that cannot split a write.
func (b *Bucket) Wait(n int) {
	for {
		allowed, delay := b.Allowance(n)
		if allowed >= n || delay <= 0 {
			return
		}
		n -= allowed
		time.Sleep(delay)
	}
}

// Snapshot is what the panel and `status --json` read.
func (b *Bucket) Snapshot() map[string]any {
	if b == nil {
		return map[string]any{"rate": 0, "burst": 0, "tokens": 0.0, "name": ""}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.refill(time.Now())
	return map[string]any{
		"rate": b.Rate, "burst": b.Burst, "tokens": b.tokens, "name": b.Name,
		"waited": round1(b.waited), "passed": b.takenIn,
	}
}

func round1(v float64) float64 {
	return float64(int64(v*10+0.5)) / 10
}

// Limiter builds a bucket from a config value: 0 / "" / null (off), a byte
// count, a size string ("20M", "500k"), or a table {rate = "10M", burst = "2M"}.
func Limiter(value any, name string) *Bucket {
	if value == nil {
		return nil
	}
	burst := int64(0)
	switch v := value.(type) {
	case string:
		if strings.TrimSpace(v) == "" {
			return nil
		}
	case bool:
		if !v {
			return nil
		}
	case map[string]any:
		burst = ParseSize(v["burst"])
		value = v["rate"]
	case map[any]any:
		burst = ParseSize(v["burst"])
		value = v["rate"]
	}
	rate := ParseSize(value)
	if rate <= 0 {
		return nil
	}
	return NewBucket(rate, burst, name)
}

// ParseSize reads "10M", "500k", "2G", "1.5MiB" or a number into bytes.
// It accepts exactly what simurgh.util.parse_size accepts.
func ParseSize(value any) int64 {
	switch v := value.(type) {
	case nil:
		return 0
	case int:
		return int64(v)
	case int64:
		return v
	case float64:
		return int64(v)
	case bool:
		return 0
	}
	text := strings.ToLower(strings.ReplaceAll(strings.TrimSpace(toString(value)), " ", ""))
	if text == "" {
		return 0
	}
	mult := int64(1)
	for _, suffix := range []struct {
		name   string
		factor int64
	}{
		{"kib", 1 << 10}, {"kb", 1 << 10}, {"k", 1 << 10},
		{"mib", 1 << 20}, {"mb", 1 << 20}, {"m", 1 << 20},
		{"gib", 1 << 30}, {"gb", 1 << 30}, {"g", 1 << 30},
		{"tib", 1 << 40}, {"tb", 1 << 40}, {"t", 1 << 40},
	} {
		if strings.HasSuffix(text, suffix.name) {
			mult = suffix.factor
			text = strings.TrimSuffix(text, suffix.name)
			break
		}
	}
	number, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return 0
	}
	return int64(number * float64(mult))
}

func toString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	}
	return ""
}

// Human renders a byte count the way the panel does.
func Human(n float64) string {
	units := []string{"B", "KB", "MB", "GB", "TB", "PB"}
	for _, unit := range units {
		if n < 1024 || unit == "PB" {
			if unit == "B" {
				return strconv.FormatInt(int64(n), 10) + " B"
			}
			return strconv.FormatFloat(n, 'f', 1, 64) + " " + unit
		}
		n /= 1024
	}
	return strconv.FormatFloat(n, 'f', 1, 64) + " PB"
}
