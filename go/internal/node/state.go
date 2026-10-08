package node

import (
	"encoding/json"
	"log/slog"
	"os"
	"sync"
	"time"
)

// state.json is the little file both engines keep so the web panel and
// `simurgh status` can show live numbers even when the panel runs in its own
// process (which it does with the Go engine: the panel stays Python).
//
// The Python engine writes `state[role] = payload` and keeps the rest of the
// file (the panel credentials live there too), so the Go writer does the same
// merge under a lock and renames the new file into place.
type stateWriter struct {
	path     string
	home     string
	role     string
	interval time.Duration

	mu      sync.Mutex
	lastAt  time.Time
	lastIn  int64
	lastOut int64
	inRate  float64
	outRate float64
	series  []map[string]any
}

func newStateWriter(home, role string, interval time.Duration) *stateWriter {
	if interval <= 0 {
		interval = 3 * time.Second
	}
	return &stateWriter{path: home + "/state.json", home: home, role: role, interval: interval}
}

// loop writes a snapshot until stop is closed.
func (w *stateWriter) loop(stop <-chan struct{}, snapshot func() map[string]any) {
	ticker := time.NewTicker(w.interval)
	defer ticker.Stop()
	w.write(snapshot)
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			w.write(snapshot)
		}
	}
}

func (w *stateWriter) write(snapshot func() map[string]any) {
	payload := snapshot()
	if payload == nil {
		return
	}
	w.mu.Lock()
	w.fillRates(payload)
	st := readStateFile(w.path)
	st[w.role] = payload
	st["updated"] = float64(time.Now().UnixMilli()) / 1000.0
	w.mu.Unlock()
	raw, err := json.MarshalIndent(st, "", " ")
	if err != nil {
		return
	}
	tmp := w.path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		slog.Debug("cannot write the state file", "err", err)
		return
	}
	if err := os.Rename(tmp, w.path); err != nil {
		slog.Debug("cannot replace the state file", "err", err)
	}
}

// fillRates adds the same rate block the Python meter produces, so the panel
// shows live speed whichever engine answers.
func (w *stateWriter) fillRates(payload map[string]any) {
	stats, _ := payload["stats"].(map[string]any)
	if stats == nil {
		stats = map[string]any{}
		payload["stats"] = stats
	}
	totals, _ := stats["totals"].(map[string]any)
	if totals == nil {
		totals = map[string]any{}
		stats["totals"] = totals
	}
	in := asInt64(totals["in"])
	out := asInt64(totals["out"])
	now := time.Now()
	if !w.lastAt.IsZero() {
		dt := now.Sub(w.lastAt).Seconds()
		if dt > 0.25 {
			w.inRate = float64(in-w.lastIn) / dt
			w.outRate = float64(out-w.lastOut) / dt
		}
	}
	w.lastAt, w.lastIn, w.lastOut = now, in, out
	if w.inRate < 0 {
		w.inRate = 0
	}
	if w.outRate < 0 {
		w.outRate = 0
	}
	stats["rates"] = map[string]any{
		"in_rate":   w.inRate,
		"out_rate":  w.outRate,
		"in_total":  in,
		"out_total": out,
	}
	// a short history for the panel chart
	w.series = append(w.series, map[string]any{
		"t": float64(now.UnixMilli()) / 1000.0, "in": in, "out": out,
	})
	if len(w.series) > 120 {
		w.series = w.series[len(w.series)-120:]
	}
	stats["series"] = w.series
	stats["uptime"] = payload["uptime"]
}

func readStateFile(path string) map[string]any {
	out := map[string]any{}
	raw, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return map[string]any{}
	}
	return out
}

func asInt64(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	}
	return 0
}
