package carrier

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/binary"
	"io"
	"net"
	"strconv"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/proto"
)

const rawTestToken = "raw-test-token"

// rawServer starts a `raw` carrier server and hands every accepted channel to
// a callback.
func rawServer(t *testing.T, token string, padding bool) (int, <-chan mux.Channel) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { ln.Close() })
	channels := make(chan mux.Channel, 8)
	srv := NewServer("raw", token, tls.Certificate{}, "close", "")
	srv.Padding = padding
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go srv.Serve(conn, func(ch mux.Channel) { channels <- ch })
		}
	}()
	return ln.Addr().(*net.TCPAddr).Port, channels
}

func rawDial(t *testing.T, port int, token string, padding bool) (mux.Channel, error) {
	t.Helper()
	return Dial(ClientOptions{
		Carrier: "raw", Address: "127.0.0.1", Port: port,
		Token: token, Padding: padding, Timeout: 5 * time.Second,
	})
}

// wirePair is a deterministic in-memory replacement for the two sockets: what
// one channel writes lands in the other one's reader, so a test can look at the
// exact record sizes on the wire.
func wirePair(t *testing.T, pad bool) (client, server *rawChannel, clientToServer *bytes.Buffer) {
	t.Helper()
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		t.Fatal(err)
	}
	clientToServer = &bytes.Buffer{}
	serverToClient := &bytes.Buffer{}
	send := &fakeConn{out: clientToServer, in: serverToClient}
	recv := &fakeConn{out: serverToClient, in: clientToServer}
	client = newRawChannel(send, key, key, pad)
	server = newRawChannel(recv, key, key, pad)
	return client, server, clientToServer
}

// fakeConn is a net.Conn whose writes land in a buffer and whose reads come out
// of another one.
type fakeConn struct {
	out *bytes.Buffer // where Write puts bytes
	in  *bytes.Buffer // where Read takes them from
}

func (c *fakeConn) Read(b []byte) (int, error) {
	if c.in == nil || c.in.Len() == 0 {
		return 0, io.EOF
	}
	return c.in.Read(b)
}
func (c *fakeConn) Write(b []byte) (int, error) {
	if c.out == nil {
		return len(b), nil
	}
	return c.out.Write(b)
}
func (c *fakeConn) Close() error                     { return nil }
func (c *fakeConn) LocalAddr() net.Addr              { return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)} }
func (c *fakeConn) RemoteAddr() net.Addr             { return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)} }
func (c *fakeConn) SetDeadline(time.Time) error      { return nil }
func (c *fakeConn) SetReadDeadline(time.Time) error  { return nil }
func (c *fakeConn) SetWriteDeadline(time.Time) error { return nil }

// A frame written by one side comes out of the other unchanged, in both
// directions, and a wrong token never gets that far.
func TestRawHandshakeAndFrames(t *testing.T) {
	port, channels := rawServer(t, rawTestToken, true)
	client, err := rawDial(t, port, rawTestToken, true)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()
	if client.Name() != "raw+aead" {
		t.Fatalf("channel name = %q", client.Name())
	}

	var server mux.Channel
	select {
	case server = <-channels:
	case <-time.After(5 * time.Second):
		t.Fatal("the server never handed over a channel")
	}
	defer server.Close()

	// client -> server: a control frame and a data frame
	if err := client.WriteFrame(proto.Frame(proto.TOpen, 7, []byte("hello"))); err != nil {
		t.Fatalf("write frame: %v", err)
	}
	hdr := proto.PutHeader(make([]byte, proto.HeaderLen), proto.TData, 7)
	payload := bytes.Repeat([]byte("abcdefgh"), 4096) // 32 KiB
	if err := client.WriteData(hdr, payload); err != nil {
		t.Fatalf("write data: %v", err)
	}

	typ, sid, body, err := server.ReadFrame()
	if err != nil || typ != proto.TOpen || sid != 7 || string(body) != "hello" {
		t.Fatalf("control frame came out as %v %d %q %v", typ, sid, body, err)
	}
	typ, sid, body, err = server.ReadFrame()
	if err != nil || typ != proto.TData || sid != 7 {
		t.Fatalf("data frame header came out as %v %d %v", typ, sid, err)
	}
	if !bytes.Equal(body, payload) {
		t.Fatalf("payload changed: %d bytes, want %d", len(body), len(payload))
	}

	// server -> client
	if err := server.WriteFrame(proto.Frame(proto.TEof, 7, nil)); err != nil {
		t.Fatalf("server write: %v", err)
	}
	typ, sid, _, err = client.ReadFrame()
	if err != nil || typ != proto.TEof || sid != 7 {
		t.Fatalf("reply frame came out as %v %d %v", typ, sid, err)
	}
}

// Padding hides exact sizes: every record lands on a bucket (or a multiple of
// 64 above them) plus the 16 byte tag, and the receiver still gets the exact
// payload back.
func TestRawPaddingHidesSizes(t *testing.T) {
	for _, size := range []int{0, 1, 100, 126, 127, 1000, 20000} {
		client, server, wire := wirePair(t, true)
		hdr := proto.PutHeader(make([]byte, proto.HeaderLen), proto.TData, 9)
		payload := bytes.Repeat([]byte("z"), size)
		if err := client.WriteData(hdr, payload); err != nil {
			t.Fatalf("write %d: %v", size, err)
		}
		record := int(binary.BigEndian.Uint32(wire.Bytes()[:4]))
		if want := padTarget(proto.HeaderLen+size+2) + 16; record != want {
			t.Fatalf("a %d byte frame went out as %d, want %d", size, record, want)
		}
		if wire.Len() != 4+record {
			t.Fatalf("record length field says %d, the write was %d", record, wire.Len()-4)
		}
		typ, sid, body, err := server.ReadFrame()
		if err != nil || typ != proto.TData || sid != 9 {
			t.Fatalf("frame of %d bytes came out as %v %d %v", size, typ, sid, err)
		}
		if len(body) != size || !bytes.Equal(body, payload) {
			t.Fatalf("payload of %d bytes came back as %d", size, len(body))
		}
	}
}

// With padding off the record is exactly the frame plus the 2 byte trailer and
// the tag, and frames still survive the round trip.
func TestRawWithoutPadding(t *testing.T) {
	client, server, wire := wirePair(t, false)
	frame := proto.Frame(proto.TData, 3, bytes.Repeat([]byte("q"), 300))
	if err := client.WriteFrame(frame); err != nil {
		t.Fatalf("write: %v", err)
	}
	record := int(binary.BigEndian.Uint32(wire.Bytes()[:4]))
	if want := len(frame) + 2 + 16; record != want {
		t.Fatalf("record = %d, want %d", record, want)
	}
	typ, sid, body, err := server.ReadFrame()
	if err != nil || typ != proto.TData || sid != 3 || len(body) != 300 {
		t.Fatalf("frame = %v %d %d %v", typ, sid, len(body), err)
	}

	// and back the other way, so both key halves are exercised
	if err := server.WriteFrame(proto.Frame(proto.TOpen, 4, []byte("ok"))); err != nil {
		t.Fatalf("server write: %v", err)
	}
	typ, sid, body, err = client.ReadFrame()
	if err != nil || typ != proto.TOpen || sid != 4 || string(body) != "ok" {
		t.Fatalf("reverse frame = %v %d %q %v", typ, sid, body, err)
	}
}

// The bucket table itself: the sizes the Python engine produces for the same
// frame must come out identical here.
func TestRawPaddingSizesMatchThePythonEngine(t *testing.T) {
	// the same numbers simurgh/carriers.py produces for the same frames
	cases := []struct{ need, want int }{
		{0, 128}, {1, 128}, {126, 128}, {127, 128}, {128, 128},
		{129, 256}, {255, 256}, {256, 256}, {257, 512},
		{16383, 16384}, {16384, 16384}, {16385, 16448},
		{16448, 16512}, {16449, 16512},
	}
	for _, c := range cases {
		if got := padTarget(c.need); got != c.want {
			t.Fatalf("padTarget(%d) = %d, want %d", c.need, got, c.want)
		}
	}
}

// A wrong token, a tampered hello and a stale timestamp all end the same way:
// random bytes back, connection gone, nothing learned.
func TestRawRejectsBadHandshakes(t *testing.T) {
	port, channels := rawServer(t, rawTestToken, true)

	// 1. wrong token: the client must refuse the server's reply, and the
	//    server must not hand over a channel
	if _, err := rawDial(t, port, "not-the-token", true); err == nil {
		t.Fatal("a wrong token was accepted")
	}
	select {
	case <-channels:
		t.Fatal("the server handed over a channel for a wrong token")
	case <-time.After(300 * time.Millisecond):
	}

	// 2. tampered hello: a valid length prefix and a valid looking body, but
	//    the tag does not match
	conn, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	hello := make([]byte, 1+4+32+8+16)
	if _, err := rand.Read(hello); err != nil {
		t.Fatal(err)
	}
	hello[0] = 4 // a valid noise prefix length
	if _, err := conn.Write(hello); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 128)
	n, err := conn.Read(buf)
	if err != nil || n != 64 {
		t.Fatalf("prober answer = %d bytes, %v (want 64 bytes of noise)", n, err)
	}

	// 3. stale timestamp with an otherwise perfect tag
	key := proto.AuthKey(rawTestToken)
	ts := time.Now().Add(-10 * time.Minute).Unix()
	var tsBuf [8]byte
	binary.BigEndian.PutUint64(tsBuf[:], uint64(ts))
	pub := make([]byte, 32)
	if _, err := rand.Read(pub); err != nil {
		t.Fatal(err)
	}
	h := hmac.New(sha256.New, key[:])
	h.Write([]byte("simurgh/v2/raw"))
	h.Write(tsBuf[:])
	h.Write(pub)
	tag := h.Sum(nil)[:16]
	stale := append([]byte{0}, pub...)
	stale = append(stale, tsBuf[:]...)
	stale = append(stale, tag...)
	conn2, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn2.Close()
	if _, err := conn2.Write(stale); err != nil {
		t.Fatal(err)
	}
	_ = conn2.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err = conn2.Read(buf)
	if err != nil || n != 64 {
		t.Fatalf("stale stamp answer = %d bytes, %v (want noise)", n, err)
	}

	select {
	case <-channels:
		t.Fatal("the server accepted a broken handshake")
	default:
	}
}

// A prober that sends something too long for our prefix, or plain HTTP, gets
// noise (or, for HTTP, the decoy) and never a tunnel.
func TestRawProberGetsNoise(t *testing.T) {
	port, _ := rawServer(t, rawTestToken, true)
	conn, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	if _, err := conn.Write(append([]byte{0xF0}, bytes.Repeat([]byte("A"), 200)...)); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 256)
	n, err := conn.Read(buf)
	if err != nil || n != 64 {
		t.Fatalf("long prefix answer = %d bytes, %v", n, err)
	}
	// the answer must not look like our handshake reply
	if bytes.Contains(buf[:n], []byte("simurgh")) {
		t.Fatalf("noise leaked a protocol string: %q", buf[:n])
	}
}

// splitConn must hand the first write to the socket in two pieces (and only
// the first), which is what gets a ClientHello past segment-level inspection.
func TestSplitFirstWriteOnly(t *testing.T) {
	rec := &recordingConn{}
	conn := splitFirst(rec, 1)
	pkt := []byte("hello world, this is the first write")
	if n, err := conn.Write(pkt); err != nil || n != len(pkt) {
		t.Fatalf("write: %d %v", n, err)
	}
	if len(rec.writes) != 2 || len(rec.writes[0]) != 1 || !bytes.Equal(rec.writes[1], pkt[1:]) {
		t.Fatalf("first write was split as %v", sizesOf(rec.writes))
	}
	// everything after that goes out in one piece
	if _, err := conn.Write([]byte("second")); err != nil {
		t.Fatal(err)
	}
	if len(rec.writes) != 3 || string(rec.writes[2]) != "second" {
		t.Fatalf("second write: %v", sizesOf(rec.writes))
	}
	if got := string(rec.flat()); got != string(pkt)+"second" {
		t.Fatalf("bytes on the wire changed: %q", got)
	}

	// a write that already fits in the first piece is not delayed, and a
	// disabled splitter returns the connection untouched
	small := &recordingConn{}
	if _, err := splitFirst(small, 64).Write([]byte("fits")); err != nil {
		t.Fatal(err)
	}
	if len(small.writes) != 1 {
		t.Fatalf("a small first write was split: %v", sizesOf(small.writes))
	}
	plain := splitFirst(&recordingConn{}, 0)
	if _, isSplit := plain.(*splitConn); isSplit {
		t.Fatal("split = 0 must not wrap the connection")
	}
}

func sizesOf(chunks [][]byte) []int {
	out := make([]int, 0, len(chunks))
	for _, c := range chunks {
		out = append(out, len(c))
	}
	return out
}

type recordingConn struct {
	writes [][]byte
}

func (c *recordingConn) Read(b []byte) (int, error) { return 0, io.EOF }
func (c *recordingConn) Write(b []byte) (int, error) {
	c.writes = append(c.writes, append([]byte(nil), b...))
	return len(b), nil
}
func (c *recordingConn) Close() error                     { return nil }
func (c *recordingConn) LocalAddr() net.Addr              { return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)} }
func (c *recordingConn) RemoteAddr() net.Addr             { return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)} }
func (c *recordingConn) SetDeadline(time.Time) error      { return nil }
func (c *recordingConn) SetReadDeadline(time.Time) error  { return nil }
func (c *recordingConn) SetWriteDeadline(time.Time) error { return nil }

func (c *recordingConn) flat() []byte {
	out := []byte{}
	for _, w := range c.writes {
		out = append(out, w...)
	}
	return out
}
