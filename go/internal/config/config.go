// Package config reads the same relay.toml / exit.toml files the Python engine
// writes, so both engines can share one configuration.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// ListenSpec is one exit listening endpoint (or, in reverse mode, one outbound
// dial the exit keeps alive).
type ListenSpec struct {
	Carrier            string
	Host               string
	Port               int
	Path               string
	Fallback           string
	DecoyFile          string
	Padding            bool
	Split              int // tls/wss: bytes in the first TCP segment of the ClientHello
	Enabled            bool
	Dial               string // reverse mode: the relay address to dial
	Fingerprint        string
	InsecureSkipVerify bool
	CertFile           string
	KeyFile            string
	MTU                int    // udp carrier: datagram budget
	FEC                string // udp carrier: "off", "3" or "10:2"
	Window             int    // udp carrier: packets in flight
}

// Mapping is one port forwarding on the relay.
type Mapping struct {
	Name        string
	Listen      int
	ListenHost  string
	TargetHost  string
	TargetPort  int
	UDP         bool
	Enabled     bool
	RateLimit   any    // "20M", a number of bytes/s, or {rate, burst}
	Quota       any    // "200G" or {bytes, period, action}
	QuotaPeriod string // day | month | total
	QuotaAction string // block | throttle | warn
	MaxConns    int    // concurrent user connections (0 = unlimited)
}

// Key is the ledger/limiter key of one mapping.  It matches the Python
// engine's Mapping.key() so both engines write the same traffic.json rows.
func (m Mapping) Key() string {
	proto := "tcp"
	if m.UDP {
		proto = "udp"
	}
	return fmt.Sprintf("%s:%s:%d/%s:%d", proto, m.ListenHost, m.Listen,
		m.TargetHost, m.TargetPort)
}

// Endpoint is a foreign server the relay can dial.
type Endpoint struct {
	Carrier            string
	Address            string
	Port               int
	Domain             string
	Path               string
	Fingerprint        string
	InsecureSkipVerify bool
	Padding            bool
	Split              int // tls/wss: bytes in the first TCP segment of the ClientHello
	Enabled            bool
	TCPNoDelay         *bool  // nil = inherit the file's value
	TCPKeepalive       *int   // nil = inherit; seconds, 0 = off
	MTU                int    // udp carrier
	FEC                string // udp carrier
	Window             int    // udp carrier
	// filled in by the parsers with the file-level values, so a dial can
	// resolve an endpoint's overrides without carrying the whole config
	FileNoDelay   bool
	FileKeepalive int
}

// Tunnel is the relay's own listener in reverse mode.
type Tunnel struct {
	Carrier   string
	Host      string
	Port      int
	Path      string
	CertFile  string
	KeyFile   string
	CertAuto  bool
	Fallback  string
	DecoyFile string
	Padding   bool
	Enabled   bool
	MTU       int    // udp carrier
	FEC       string // udp carrier
	Window    int    // udp carrier
}

// Token is one credential of a multi-tenant exit ([[token]] blocks).  The
// per-token policy fields are parsed here; the relay keeps them for its own
// accounting of which exit accepts what.
type Token struct {
	Value          string
	Name           string
	Ports          []int
	Targets        []string
	AllowAnyTarget bool
	RateLimit      any
	Quota          int64
	QuotaPeriod    string
	QuotaAction    string
	MaxConns       int
	Enabled        bool
}

// Exit is exit.toml.
type Exit struct {
	Token           string
	Name            string
	CertAuto        bool
	CertFile        string
	KeyFile         string
	AllowIPs        []string
	PushPorts       []int
	StrictPorts     bool
	SpeedtestPort   int
	ProxyProtocol   string
	LogLevel        string
	Listen          []ListenSpec
	StreamWindow    int
	MaxStreamWindow int
	Chunk           int
	Connections     int
	Keepalive       int
	TCPNoDelay      bool // Nagle off (the Python engine's tcp_nodelay)
	TCPKeepalive    int  // keepalive idle time in seconds, 0 = off
	Tokens          []Token
}

// Relay is relay.toml.
type Relay struct {
	Token           string
	Name            string
	PanelPort       int
	Dial            string // "relay" (direct) or "exit" (reverse)
	AcceptPush      bool
	Keepalive       int
	StreamWindow    int
	MaxStreamWindow int
	Chunk           int
	Connections     int
	SpeedtestPort   int
	LogLevel        string
	TCPNoDelay      bool // Nagle off (the Python engine's tcp_nodelay)
	TCPKeepalive    int  // keepalive idle time in seconds, 0 = off
	Tokens          []Token
	RateLimit       any // node-wide fallback for a mapping without its own
	Quota           any
	QuotaPeriod     string
	QuotaAction     string
	MaxConns        int
	// Unpaired marks a relay that was installed before its exit existed
	// (`simurgh install --pair-later`): the token and the exit address are
	// still empty and the data plane waits for `simurgh join` / the panel.
	Unpaired bool
	Exit     Endpoint
	Pool     []Endpoint
	Tunnel   Tunnel
	Mappings []Mapping
}

// Defaults, matching the Python engine.
const (
	DefaultStreamWindow    = 1 << 20 // 1 MiB starting window per stream
	DefaultMaxStreamWindow = 8 << 20
	DefaultChunk           = 64 << 10
	DefaultKeepalive       = 25
	DefaultMTU             = 1200
	DefaultUDPWindow       = 1024
)

// LoadExit reads exit.toml.
func LoadExit(path string) (*Exit, error) {
	tree, err := parseFile(path)
	if err != nil {
		return nil, err
	}
	cfg := &Exit{
		Token:           str(tree, "token", ""),
		Name:            str(tree, "name", ""),
		CertAuto:        boolean(tree, "cert_auto", true),
		CertFile:        str(tree, "cert_file", ""),
		KeyFile:         str(tree, "key_file", ""),
		AllowIPs:        strList(tree, "allow_ips"),
		PushPorts:       intList(tree, "push_ports"),
		StrictPorts:     boolean(tree, "strict_ports", false),
		SpeedtestPort:   integer(tree, "speedtest_port", 8808),
		ProxyProtocol:   str(tree, "proxy_protocol", "off"),
		LogLevel:        str(tree, "log_level", "info"),
		StreamWindow:    clampInt(integer(tree, "stream_window", DefaultStreamWindow), 16<<10, 1<<30),
		MaxStreamWindow: integer(tree, "max_stream_window", DefaultMaxStreamWindow),
		Chunk:           clampInt(integer(tree, "chunk", DefaultChunk), 4096, 1<<20),
		Connections:     clampInt(integer(tree, "connections", 1), 1, 16),
		Keepalive:       clampInt(integer(tree, "keepalive", DefaultKeepalive), 5, 3600),
		TCPNoDelay:      boolean(tree, "tcp_nodelay", true),
		TCPKeepalive:    maxInt(0, integer(tree, "tcp_keepalive", 0)),
		Tokens:          tokenList(tree, str(tree, "token", "")),
	}
	if cfg.MaxStreamWindow < cfg.StreamWindow {
		cfg.MaxStreamWindow = cfg.StreamWindow
	}
	if cfg.Token == "" {
		return nil, fmt.Errorf("exit config: 'token' is required")
	}
	for _, item := range tables(tree, "listen") {
		cfg.Listen = append(cfg.Listen, ListenSpec{
			Carrier:            str(item, "carrier", "tls"),
			Host:               str(item, "host", "0.0.0.0"),
			Port:               integer(item, "port", 8443),
			Path:               str(item, "path", "/ws"),
			Fallback:           str(item, "fallback", "decoy"),
			DecoyFile:          str(item, "decoy_file", ""),
			Padding:            boolean(item, "padding", true),
			Split:              integer(item, "split", 0),
			Enabled:            boolean(item, "enabled", true),
			Dial:               str(item, "dial", ""),
			Fingerprint:        str(item, "fingerprint", ""),
			InsecureSkipVerify: boolean(item, "insecure_skip_verify", false),
			CertFile:           str(item, "cert_file", ""),
			KeyFile:            str(item, "key_file", ""),
			MTU:                clampInt(integer(item, "mtu", DefaultMTU), 512, 65000),
			FEC:                fecSpec(str(item, "fec", "off")),
			Window:             clampInt(integer(item, "window", DefaultUDPWindow), 32, 65536),
		})
	}
	return cfg, nil
}

// LoadRelay reads relay.toml.
func LoadRelay(path string) (*Relay, error) {
	tree, err := parseFile(path)
	if err != nil {
		return nil, err
	}
	cfg := &Relay{
		Token:           str(tree, "token", ""),
		Name:            str(tree, "name", ""),
		PanelPort:       integer(tree, "panel_port", 8787),
		Dial:            strings.ToLower(str(tree, "dial", "relay")),
		AcceptPush:      boolean(tree, "accept_push", true),
		Keepalive:       clampInt(integer(tree, "keepalive", DefaultKeepalive), 5, 3600),
		StreamWindow:    clampInt(integer(tree, "stream_window", DefaultStreamWindow), 16<<10, 1<<30),
		MaxStreamWindow: integer(tree, "max_stream_window", DefaultMaxStreamWindow),
		Chunk:           clampInt(integer(tree, "chunk", DefaultChunk), 4096, 1<<20),
		Connections:     clampInt(integer(tree, "connections", 1), 1, 16),
		SpeedtestPort:   integer(tree, "speedtest_port", 0),
		LogLevel:        str(tree, "log_level", "info"),
		TCPNoDelay:      boolean(tree, "tcp_nodelay", true),
		TCPKeepalive:    maxInt(0, integer(tree, "tcp_keepalive", 0)),
		Tokens:          tokenList(tree, str(tree, "token", "")),
		RateLimit:       tree["rate_limit"],
		Quota:           tree["quota"],
		QuotaPeriod:     str(tree, "quota_period", "month"),
		QuotaAction:     str(tree, "quota_action", "block"),
		MaxConns:        integer(tree, "max_conns", 0),
		Unpaired:        boolean(tree, "unpaired", false),
	}
	if cfg.MaxStreamWindow < cfg.StreamWindow {
		cfg.MaxStreamWindow = cfg.StreamWindow
	}
	if cfg.Token == "" && !cfg.Unpaired {
		return nil, fmt.Errorf("relay config: 'token' is required " +
			"(or `unpaired = true` for a server that is not paired yet)")
	}
	if t := table(tree, "exit"); t != nil {
		cfg.Exit = endpoint(t)
	}
	// the endpoints inherit the file's socket options unless they set their own
	cfg.Exit.FileNoDelay, cfg.Exit.FileKeepalive = cfg.TCPNoDelay, cfg.TCPKeepalive
	for _, t := range tables(tree, "pool") {
		ep := endpoint(t)
		ep.FileNoDelay, ep.FileKeepalive = cfg.TCPNoDelay, cfg.TCPKeepalive
		cfg.Pool = append(cfg.Pool, ep)
	}
	if t := table(tree, "tunnel"); t != nil {
		cfg.Tunnel = Tunnel{
			Carrier:   str(t, "carrier", "tls"),
			Host:      str(t, "host", "0.0.0.0"),
			Port:      integer(t, "port", 19443),
			Path:      str(t, "path", "/ws"),
			CertFile:  str(t, "cert_file", ""),
			KeyFile:   str(t, "key_file", ""),
			CertAuto:  boolean(t, "cert_auto", true),
			Fallback:  str(t, "fallback", "decoy"),
			DecoyFile: str(t, "decoy_file", ""),
			Padding:   boolean(t, "padding", true),
			Enabled:   boolean(t, "enabled", true),
			MTU:       clampInt(integer(t, "mtu", DefaultMTU), 512, 65000),
			FEC:       fecSpec(str(t, "fec", "off")),
			Window:    clampInt(integer(t, "window", DefaultUDPWindow), 32, 65536),
		}
	}
	for _, t := range tables(tree, "mapping") {
		cfg.Mappings = append(cfg.Mappings, Mapping{
			Name:        str(t, "name", ""),
			Listen:      integer(t, "listen", 0),
			ListenHost:  str(t, "listen_host", "0.0.0.0"),
			TargetHost:  str(t, "target_host", "127.0.0.1"),
			TargetPort:  integer(t, "target_port", 0),
			UDP:         boolean(t, "udp", false),
			Enabled:     boolean(t, "enabled", true),
			RateLimit:   t["rate_limit"],
			Quota:       t["quota"],
			QuotaPeriod: str(t, "quota_period", "month"),
			QuotaAction: str(t, "quota_action", "block"),
			MaxConns:    integer(t, "max_conns", 0),
		})
	}
	if cfg.Dial == "relay" && cfg.Exit.Address == "" && !cfg.Unpaired {
		return nil, fmt.Errorf("relay config: an [exit] block with an address is required")
	}
	if cfg.Dial == "exit" && cfg.Tunnel.Port == 0 {
		return nil, fmt.Errorf("relay config: reverse mode needs a [tunnel] port")
	}
	return cfg, nil
}

// NoDelayValue is the endpoint's effective `tcp_nodelay`: its own key when it
// sets one, the file's value otherwise -- the same None means inherit rule the
// Python engine applies to [exit] and [[pool]].
func (e Endpoint) NoDelayValue() bool {
	if e.TCPNoDelay != nil {
		return *e.TCPNoDelay
	}
	return e.FileNoDelay
}

// KeepaliveValue is the effective `tcp_keepalive` in seconds (0 = off).
func (e Endpoint) KeepaliveValue() int {
	if e.TCPKeepalive != nil {
		return *e.TCPKeepalive
	}
	return e.FileKeepalive
}

// FileNoDelayValue is only the file-level value, ignoring the endpoint's own
// key: that is what a sibling endpoint inherits from the file.
func (e Endpoint) FileNoDelayValue() bool { return e.FileNoDelay }

// FileKeepaliveValue is only the file-level `tcp_keepalive` in seconds.
func (e Endpoint) FileKeepaliveValue() int { return e.FileKeepalive }

func endpoint(t map[string]any) Endpoint {
	return Endpoint{
		Carrier:            str(t, "carrier", "tls"),
		Address:            str(t, "address", str(t, "host", "")),
		Port:               integer(t, "port", 443),
		Domain:             str(t, "domain", ""),
		Path:               str(t, "path", "/ws"),
		Fingerprint:        str(t, "fingerprint", ""),
		InsecureSkipVerify: boolean(t, "insecure_skip_verify", false),
		Padding:            boolean(t, "padding", true),
		Split:              integer(t, "split", 0),
		Enabled:            boolean(t, "enabled", true),
		MTU:                clampInt(integer(t, "mtu", DefaultMTU), 512, 65000),
		FEC:                fecSpec(str(t, "fec", "off")),
		Window:             clampInt(integer(t, "window", DefaultUDPWindow), 32, 65536),
		TCPNoDelay:         boolPtr(t, "tcp_nodelay"),
		TCPKeepalive:       intPtr(t, "tcp_keepalive"),
	}
}

// boolPtr and intPtr keep "not configured" apart from "configured as false/0",
// which is what the Python engine's `None` does for the same keys.
func boolPtr(t map[string]any, key string) *bool {
	raw, ok := t[key]
	if !ok {
		return nil
	}
	switch value := raw.(type) {
	case bool:
		return &value
	case string:
		parsed := value == "true" || value == "1"
		return &parsed
	case int64:
		parsed := value != 0
		return &parsed
	case float64:
		parsed := value != 0
		return &parsed
	}
	return nil
}

func intPtr(t map[string]any, key string) *int {
	if _, ok := t[key]; !ok {
		return nil
	}
	value := integer(t, key, 0)
	return &value
}

// tokenList reads the exit's credentials: the plain `token` plus any
// `[[token]]` block (the Python engine writes one per extra relay).
func tokenList(tree map[string]any, primary string) []Token {
	out := []Token{}
	if primary != "" {
		out = append(out, Token{Value: primary, Name: str(tree, "token_name", ""), Enabled: true})
	}
	for _, t := range tables(tree, "tokens") {
		value := str(t, "token", str(t, "value", ""))
		if value == "" {
			continue
		}
		out = append(out, Token{
			Value:          value,
			Name:           str(t, "name", ""),
			Ports:          intList(t, "ports"),
			Targets:        strList(t, "targets"),
			AllowAnyTarget: boolean(t, "allow_any_target", false),
			RateLimit:      t["rate_limit"],
			Quota:          int64(integer(t, "quota", 0)),
			QuotaPeriod:    str(t, "quota_period", "monthly"),
			QuotaAction:    str(t, "quota_action", "block"),
			MaxConns:       integer(t, "max_conns", 0),
			Enabled:        boolean(t, "enabled", true),
		})
	}
	return out
}

// tokenValues is what the carriers authenticate against.
func (e *Exit) TokenValues() []string {
	out := make([]string, 0, len(e.Tokens))
	for _, t := range e.Tokens {
		if t.Enabled && t.Value != "" {
			out = append(out, t.Value)
		}
	}
	if len(out) == 0 && e.Token != "" {
		out = append(out, e.Token)
	}
	return out
}

// TokenValues is what the carriers authenticate against.
func (r *Relay) TokenValues() []string {
	out := make([]string, 0, len(r.Tokens))
	for _, t := range r.Tokens {
		if t.Enabled && t.Value != "" {
			out = append(out, t.Value)
		}
	}
	if len(out) == 0 && r.Token != "" {
		out = append(out, r.Token)
	}
	return out
}

// fecSpec keeps "off" verbatim and normalises the rest.
func fecSpec(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" || value == "off" || value == "none" || value == "false" {
		return "off"
	}
	return value
}

// ---------------------------------------------------------------------------
// a small TOML reader: tables, arrays of tables and scalar values

func parseFile(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parse(string(raw))
}

func parse(text string) (map[string]any, error) {
	root := map[string]any{}
	current := root
	lines := strings.Split(text, "\n")
	for i := 0; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// keep reading while an array is left open
		for strings.Count(line, "[") > strings.Count(line, "]") && i+1 < len(lines) {
			i++
			line += " " + strings.TrimSpace(lines[i])
		}
		if strings.HasPrefix(line, "[[") {
			name := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(line, "[["), "]]"))
			tbl := map[string]any{}
			list, _ := root[name].([]any)
			root[name] = append(list, tbl)
			current = tbl
			continue
		}
		if strings.HasPrefix(line, "[") {
			name := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(line, "["), "]"))
			tbl, _ := root[name].(map[string]any)
			if tbl == nil {
				tbl = map[string]any{}
				root[name] = tbl
			}
			current = tbl
			continue
		}
		eq := strings.Index(line, "=")
		if eq < 0 {
			continue
		}
		key := strings.TrimSpace(line[:eq])
		value := strings.TrimSpace(line[eq+1:])
		if idx := strings.Index(value, " #"); idx >= 0 && !strings.Contains(value[:idx], "\"") {
			value = strings.TrimSpace(value[:idx])
		}
		current[key] = parseValue(value)
	}
	return root, nil
}

func parseValue(v string) any {
	if strings.HasPrefix(v, "\"") || strings.HasPrefix(v, "'") {
		quote := v[:1]
		v = strings.TrimPrefix(v, quote)
		if idx := strings.Index(v, quote); idx >= 0 {
			v = v[:idx]
		}
		return v
	}
	if strings.HasPrefix(v, "[") {
		inner := strings.TrimSuffix(strings.TrimPrefix(v, "["), "]")
		var out []any
		for _, part := range strings.Split(inner, ",") {
			part = strings.TrimSpace(part)
			if part == "" {
				continue
			}
			out = append(out, parseValue(part))
		}
		return out
	}
	switch strings.ToLower(v) {
	case "true":
		return true
	case "false":
		return false
	}
	if n, err := strconv.ParseInt(v, 10, 64); err == nil {
		return n
	}
	if f, err := strconv.ParseFloat(v, 64); err == nil {
		return f
	}
	return strings.TrimSpace(v)
}

func str(t map[string]any, key, def string) string {
	if v, ok := t[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

func integer(t map[string]any, key string, def int) int {
	if v, ok := t[key]; ok {
		switch n := v.(type) {
		case int64:
			return int(n)
		case float64:
			return int(n)
		case string:
			if parsed, err := strconv.Atoi(n); err == nil {
				return parsed
			}
		}
	}
	return def
}

func boolean(t map[string]any, key string, def bool) bool {
	if v, ok := t[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return def
}

func strList(t map[string]any, key string) []string {
	list, _ := t[key].([]any)
	out := make([]string, 0, len(list))
	for _, item := range list {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func intList(t map[string]any, key string) []int {
	list, _ := t[key].([]any)
	out := make([]int, 0, len(list))
	for _, item := range list {
		switch n := item.(type) {
		case int64:
			out = append(out, int(n))
		}
	}
	return out
}

func table(t map[string]any, key string) map[string]any {
	tbl, _ := t[key].(map[string]any)
	return tbl
}

func tables(t map[string]any, key string) []map[string]any {
	list, _ := t[key].([]any)
	out := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if tbl, ok := item.(map[string]any); ok {
			out = append(out, tbl)
		}
	}
	return out
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
