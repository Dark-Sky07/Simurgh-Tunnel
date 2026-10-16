package config

import (
	"os"
	"path/filepath"
	"testing"
)

// One config file, two engines: these tests pin the keys `simurgh tune` writes
// and the per-endpoint overrides the Python engine has always had.

func write(t *testing.T, name, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
	return path
}

func TestSocketOptionsHaveTheSameDefaultsAsPython(t *testing.T) {
	path := write(t, "exit.toml", "token = \"tok\"\n\n[[listen]]\ncarrier = \"raw\"\nport = 8080\n")
	cfg, err := LoadExit(path)
	if err != nil {
		t.Fatalf("load exit: %v", err)
	}
	if !cfg.TCPNoDelay {
		t.Fatal("tcp_nodelay must default to true, like the Python engine")
	}
	if cfg.TCPKeepalive != 0 {
		t.Fatalf("tcp_keepalive must default to 0 (off), got %d", cfg.TCPKeepalive)
	}
}

func TestProfileKeysAreReadFromBothFiles(t *testing.T) {
	relay := write(t, "relay.toml", `token = "tok"
stream_window = 131072
max_stream_window = 2097152
chunk = 16384
connections = 4
keepalive = 10
tcp_nodelay = false
tcp_keepalive = 15

[exit]
carrier = "raw"
address = "127.0.0.1"
port = 8443
`)
	cfg, err := LoadRelay(relay)
	if err != nil {
		t.Fatalf("load relay: %v", err)
	}
	if cfg.StreamWindow != 131072 || cfg.MaxStreamWindow != 2097152 {
		t.Fatalf("windows not read: %d / %d", cfg.StreamWindow, cfg.MaxStreamWindow)
	}
	if cfg.Chunk != 16384 || cfg.Connections != 4 || cfg.Keepalive != 10 {
		t.Fatalf("profile keys not read: chunk=%d connections=%d keepalive=%d",
			cfg.Chunk, cfg.Connections, cfg.Keepalive)
	}
	if cfg.TCPNoDelay || cfg.TCPKeepalive != 15 {
		t.Fatalf("socket options not read: nodelay=%v keepalive=%d",
			cfg.TCPNoDelay, cfg.TCPKeepalive)
	}
}

func TestAnEndpointOverridesTheFileLevelSocketOptions(t *testing.T) {
	relay := write(t, "relay.toml", `token = "tok"
tcp_nodelay = true
tcp_keepalive = 15

[exit]
carrier = "raw"
address = "127.0.0.1"
port = 8443
tcp_nodelay = false
tcp_keepalive = 0

[[pool]]
carrier = "raw"
address = "127.0.0.2"
port = 8443
`)
	cfg, err := LoadRelay(relay)
	if err != nil {
		t.Fatalf("load relay: %v", err)
	}
	// the [exit] block sets its own values
	if got := cfg.Exit.NoDelayValue(); got {
		t.Fatal("[exit] tcp_nodelay = false was ignored")
	}
	if got := cfg.Exit.KeepaliveValue(); got != 0 {
		t.Fatalf("[exit] tcp_keepalive = 0 was ignored: %d", got)
	}
	// ... while a pool entry without them inherits the file's
	if len(cfg.Pool) != 1 {
		t.Fatalf("pool: %d entries", len(cfg.Pool))
	}
	if got := cfg.Pool[0].NoDelayValue(); !got {
		t.Fatal("a pool entry must inherit the file's tcp_nodelay")
	}
	if got := cfg.Pool[0].KeepaliveValue(); got != 15 {
		t.Fatalf("a pool entry must inherit the file's tcp_keepalive: %d", got)
	}
}

func TestAnUnsetEndpointOverrideIsNotTheSameAsFalse(t *testing.T) {
	// `tcp_nodelay` absent must inherit; `tcp_nodelay = false` must not.
	plain := write(t, "plain.toml", `token = "tok"

[exit]
carrier = "raw"
address = "127.0.0.1"
port = 8443
`)
	cfg, err := LoadRelay(plain)
	if err != nil {
		t.Fatalf("load relay: %v", err)
	}
	if cfg.Exit.TCPNoDelay != nil || cfg.Exit.TCPKeepalive != nil {
		t.Fatal("an absent override must stay nil, not become false/0")
	}
	if !cfg.Exit.NoDelayValue() {
		t.Fatal("an absent override must inherit the file value (true)")
	}

	falsey := write(t, "falsey.toml", `token = "tok"

[exit]
carrier = "raw"
address = "127.0.0.1"
port = 8443
tcp_nodelay = false
`)
	cfg, err = LoadRelay(falsey)
	if err != nil {
		t.Fatalf("load relay: %v", err)
	}
	if cfg.Exit.TCPNoDelay == nil || *cfg.Exit.TCPNoDelay {
		t.Fatal("tcp_nodelay = false must be an explicit false")
	}
	if cfg.Exit.NoDelayValue() {
		t.Fatal("the explicit false must win over the file value")
	}
}
