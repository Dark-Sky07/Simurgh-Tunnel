package carrier

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
)

// The decoy is what a prober sees. These tests pin the behaviour: a browser,
// a scanner or a smart filter that knocks without the token must be answered
// exactly like a small nginx site would answer, never like a tunnel.

func TestLooksLikeHTTP(t *testing.T) {
	cases := []struct {
		peek string
		want bool
	}{
		{"GET", true}, {"get", true}, {"HEAD", true}, {"POST", true},
		{"PUT", true}, {"OPTIONS", true}, {"DELETE", true}, {"PATCH", true},
		{"", false}, {"G", false},
		// a tunnel handshake: version byte, timestamp, HMAC tail
		{"\x01\x66\x00", false},
		{"XYZ", false},
	}
	for _, c := range cases {
		if got := looksLikeHTTP([]byte(c.peek)); got != c.want {
			t.Errorf("looksLikeHTTP(%q) = %v, want %v", c.peek, got, c.want)
		}
	}
}

func TestRequestPath(t *testing.T) {
	cases := []struct {
		head string
		want string
	}{
		{"GET / HTTP/1.1\r\nHost: x\r\n\r\n", "/"},
		{"GET /index.html HTTP/1.0\r\n\r\n", "/index.html"},
		{"HEAD /a?b=c HTTP/1.1\r\n\r\n", "/a?b=c"},
		{"nonsense no path\r\n\r\n", ""},
		{"BOGUS /x HTTP/1.1\r\n\r\n", ""},
	}
	for _, c := range cases {
		if got := requestPath([]byte(c.head)); got != c.want {
			t.Errorf("requestPath(%q) = %q, want %q", c.head, got, c.want)
		}
	}
}

func selfSigned(t *testing.T) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "decoy.test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		DNSNames:     []string{"decoy.test"},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("cert: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("key der: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatalf("keypair: %v", err)
	}
	return cert
}

type decoyHarness struct {
	addr string
}

// startServer runs a real carrier server on a loopback port. onChannel records
// that a peer authenticated, which must never happen for a probe.
func startServer(t *testing.T, name, fallback, token string, cert tls.Certificate) (*decoyHarness, chan struct{}) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	h := &decoyHarness{addr: ln.Addr().String()}
	authed := make(chan struct{}, 4)
	srv := NewServer(name, token, cert, fallback, "")
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go srv.Serve(conn, func(mux.Channel) {
				select {
				case authed <- struct{}{}:
				default:
				}
			})
		}
	}()
	t.Cleanup(func() { ln.Close() })
	return h, authed
}

func readAllWithin(t *testing.T, conn net.Conn, limit int, wait time.Duration) string {
	t.Helper()
	conn.SetReadDeadline(time.Now().Add(wait))
	buf := make([]byte, 0, 4096)
	tmp := make([]byte, 2048)
	for len(buf) < limit {
		n, err := conn.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			if strings.Contains(string(buf), "</html>") {
				break
			}
		}
		if err != nil {
			break
		}
	}
	return string(buf)
}

func TestPlainProbeSeesTheDecoySite(t *testing.T) {
	h, authed := startServer(t, "plain", "decoy", "secret-token", tls.Certificate{})
	conn, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET / HTTP/1.1\r\nHost: example.com\r\nUser-Agent: curl/8\r\n\r\n")
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.HasPrefix(body, "HTTP/1.1 200 OK") {
		t.Fatalf("probe got %q, want an HTTP 200 page", firstLine(body))
	}
	if !strings.Contains(body, "Welcome to nginx!") {
		t.Fatalf("decoy page missing from response: %q", firstLine(body))
	}
	select {
	case <-authed:
		t.Fatal("a probe without the token was let into the tunnel")
	default:
	}
}

func TestPlainProbeMissingPathIs404(t *testing.T) {
	h, _ := startServer(t, "plain", "decoy", "secret-token", tls.Certificate{})
	conn, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET /wp-login.php HTTP/1.1\r\nHost: example.com\r\n\r\n")
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.HasPrefix(body, "HTTP/1.1 404 Not Found") {
		t.Fatalf("scanner got %q, want a 404 like any small site", firstLine(body))
	}
}

func TestPlainWrongTokenSeesTheDecoy(t *testing.T) {
	h, authed := startServer(t, "plain", "decoy", "secret-token", tls.Certificate{})
	conn, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	// A client that speaks the handshake shape with a wrong HMAC, then stops.
	// A real web server answers "400 Bad Request" to binary junk; so do we.
	junk := append([]byte{1, 0, 0, 0, 0, 0, 0, 0, 0}, make([]byte, 16)...)
	conn.Write(junk)
	conn.(*net.TCPConn).CloseWrite()
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.HasPrefix(body, "HTTP/1.1 400 Bad Request") {
		t.Fatalf("wrong token got %q, want the same 400 a web server would send", firstLine(body))
	}
	select {
	case <-authed:
		t.Fatal("a wrong token was let into the tunnel")
	default:
	}
}

func TestDecoyOverTLS(t *testing.T) {
	cert := selfSigned(t)
	h, authed := startServer(t, "tls", "decoy", "secret-token", cert)
	conn, err := tls.Dial("tcp", h.addr, &tls.Config{InsecureSkipVerify: true})
	if err != nil {
		t.Fatalf("tls dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.HasPrefix(body, "HTTP/1.1 200 OK") || !strings.Contains(body, "Welcome to nginx!") {
		t.Fatalf("tls probe got %q, want the decoy page", firstLine(body))
	}
	select {
	case <-authed:
		t.Fatal("an unauthenticated TLS probe was let into the tunnel")
	default:
	}
}

func TestFallbackCloseSaysNothing(t *testing.T) {
	h, _ := startServer(t, "plain", "close", "secret-token", tls.Certificate{})
	conn, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
	conn.(*net.TCPConn).CloseWrite()
	body := readAllWithin(t, conn, 1<<20, 3*time.Second)
	if body != "" {
		t.Fatalf("fallback=close sent %q, want silence", firstLine(body))
	}
}

func TestFallbackProxiesARealSite(t *testing.T) {
	// a tiny stand-in website the decoy is told to proxy to
	site, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("site listen: %v", err)
	}
	defer site.Close()
	go func() {
		for {
			c, err := site.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				buf := make([]byte, 1024)
				c.SetReadDeadline(time.Now().Add(3 * time.Second))
				_, _ = c.Read(buf)
				io.WriteString(c, "HTTP/1.1 200 OK\r\nServer: real-site\r\nContent-Length: 12\r\n\r\nhello world!")
			}(c)
		}
	}()

	h, authed := startServer(t, "plain", "site:"+site.Addr().String(), "secret-token", tls.Certificate{})
	conn, err := net.Dial("tcp", h.addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET / HTTP/1.1\r\nHost: real-site\r\n\r\n")
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.Contains(body, "hello world!") {
		t.Fatalf("site fallback returned %q, want the real site", firstLine(body))
	}
	select {
	case <-authed:
		t.Fatal("a probe was let into the tunnel")
	default:
	}
}

func TestCustomDecoyPage(t *testing.T) {
	dir := t.TempDir()
	page := dir + "/index.html"
	if err := os.WriteFile(page, []byte("<html><body>My Company</body></html>"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	srv := NewServer("plain", "tok", tls.Certificate{}, "decoy", page)
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go srv.Serve(c, func(mux.Channel) {})
		}
	}()
	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	io.WriteString(conn, "GET / HTTP/1.1\r\nHost: x\r\n\r\n")
	body := readAllWithin(t, conn, 1<<20, 5*time.Second)
	if !strings.Contains(body, "My Company") {
		t.Fatalf("custom page not served: %q", firstLine(body))
	}
}

func firstLine(s string) string {
	if idx := strings.IndexByte(s, '\n'); idx >= 0 {
		return s[:idx]
	}
	if len(s) > 120 {
		return s[:120]
	}
	return s
}
