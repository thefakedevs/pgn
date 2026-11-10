package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The production HAProxy image is exercised only in CI, entirely over mounted
// Unix sockets with --network none. No host ports or production nodes are used.
func TestHAProxySharedTLSIngress(t *testing.T) {
	image := os.Getenv("PGN_TEST_HAPROXY_IMAGE")
	if image == "" {
		t.Skip("set PGN_TEST_HAPROXY_IMAGE for the isolated Docker ingress test")
	}
	if os.Geteuid() != 0 {
		t.Fatal("run the Docker fixture as root to test socket UID/GID ownership")
	}
	dir := t.TempDir()
	os.Chmod(dir, 0755)
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "pg.node.example.net"},
		DNSNames: []string{"pg.node.example.net", "node.example.net"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment | x509.KeyUsageCertSign, IsCA: true, BasicConstraintsValid: true}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	certPath, keyPath := filepath.Join(dir, "cert.pem"), filepath.Join(dir, "key.pem")
	os.WriteFile(certPath, certPEM, 0644)
	os.WriteFile(keyPath, keyPEM, 0600)
	os.WriteFile(filepath.Join(dir, "management.pem"), append(certPEM, keyPEM...), 0600)
	app := filepath.Join(dir, "fake-pgn")
	os.WriteFile(app, []byte("#!/bin/sh\nexit 0\n"), 0700)
	api, err := listenUnix(filepath.Join(dir, "maintenance.sock"), 99)
	if err != nil {
		t.Fatal(err)
	}
	apiServer := &http.Server{Handler: routes(&server{cfg: config{APIKey: "fixture-key", AppName: app}})}
	go apiServer.Serve(api)
	t.Cleanup(func() { apiServer.Close(); api.Close() })

	grpc, err := net.Listen("unix", filepath.Join(dir, "grpc.sock"))
	if err != nil {
		t.Fatal(err)
	}
	os.Chmod(filepath.Join(dir, "grpc.sock"), 0666)
	grpcServer := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor != 2 || r.Header.Get("content-type") != "application/grpc" {
			http.Error(w, "HTTP/2 required", 400)
			return
		}
		if r.Header.Get("x-api-key") != "fixture-key" {
			http.Error(w, "auth lost", 401)
			return
		}
		w.Header().Set("Content-Type", "application/grpc")
		w.Header().Set("Trailer", "Grpc-Status")
		w.Write([]byte{0, 0, 0, 0, 0})
		w.Header().Set("Grpc-Status", "0")
	})}
	go grpcServer.ServeTLS(grpc, certPath, keyPath)
	t.Cleanup(func() { grpcServer.Close(); grpc.Close() })

	reality, err := net.Listen("unix", filepath.Join(dir, "reality.sock"))
	if err != nil {
		t.Fatal(err)
	}
	os.Chmod(filepath.Join(dir, "reality.sock"), 0666)
	site := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "existing-site") })}
	go site.ServeTLS(proxyListener{reality}, certPath, keyPath)
	t.Cleanup(func() { site.Close(); reality.Close() })

	base, err := os.ReadFile("../../templates/haproxy.cfg.tpl")
	if err != nil {
		t.Fatal(err)
	}
	fragment, err := os.ReadFile("../../templates/maintenance.cfg.tpl")
	if err != nil {
		t.Fatal(err)
	}
	config := strings.NewReplacer("@@MANAGEMENT_SERVER@@", "server management /run/pgn/management.sock",
		"@@MAINTENANCE_FRONTENDS@@", string(fragment), "@@API_DOMAIN@@", "pg.node.example.net").Replace(string(base))
	config = strings.ReplaceAll(config, "@@API_DOMAIN@@", "pg.node.example.net")
	config = strings.NewReplacer(
		"bind 0.0.0.0:443", "bind /fixture/public.sock mode 666",
		"/run/pgn/", "/fixture/",
		"/etc/pgn/tls/node-ca.pem", "/fixture/cert.pem",
		"/etc/pgn/tls/management.pem", "/fixture/management.pem",
		"server node 127.0.0.1:62050 ssl", "server node /fixture/grpc.sock ssl",
		"server xray 127.0.0.1:10443 send-proxy-v2", "server xray /fixture/reality.sock send-proxy-v2").Replace(config)
	os.WriteFile(filepath.Join(dir, "haproxy.cfg"), []byte(config), 0644)
	name := fmt.Sprintf("pgn-ingress-%d", time.Now().UnixNano())
	cmd := exec.Command("docker", "run", "--rm", "--name", name, "--network", "none", "--user", "0:0",
		"--mount", "type=bind,src="+dir+",dst=/fixture", image, "haproxy", "-db", "-f", "/fixture/haproxy.cfg")
	var logs bytes.Buffer
	cmd.Stdout = &logs
	cmd.Stderr = &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	t.Cleanup(func() {
		exec.Command("docker", "rm", "-f", name).Run()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			cmd.Process.Kill()
		}
	})
	ready := false
	for i := 0; i < 100; i++ {
		if _, err := os.Stat(filepath.Join(dir, "public.sock")); err == nil {
			ready = true
			break
		}
		select {
		case err := <-done:
			t.Fatalf("HAProxy exited: %v\n%s", err, logs.String())
		default:
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ready {
		t.Fatal("HAProxy did not create the fixture listener")
	}
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(certPEM)
	newClient := func(h2 bool) *http.Client {
		tr := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}, ForceAttemptHTTP2: h2,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", filepath.Join(dir, "public.sock"))
			}}
		if !h2 {
			tr.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
		}
		t.Cleanup(tr.CloseIdleConnections)
		return &http.Client{Transport: tr, Timeout: 5 * time.Second}
	}
	h1, h2 := newClient(false), newClient(true)
	if code := tlsCall(t, h1, "GET", "https://pg.node.example.net/", "", ""); code != 401 {
		t.Fatal(code)
	}
	if code := tlsCall(t, h1, "GET", "https://pg.node.example.net/", "fixture-key", ""); code != 200 {
		t.Fatal(code)
	}
	for _, tt := range []struct{ path, body string }{
		{"/node/update", `{}`}, {"/node/core_update", `{"core_version":"26.11.9"}`}, {"/node/geofiles", `{"region":"russia"}`},
	} {
		if code := tlsCall(t, h1, "POST", "https://pg.node.example.net"+tt.path, "fixture-key", tt.body); code != 200 {
			t.Fatalf("%s=%d", tt.path, code)
		}
	}
	req, _ := http.NewRequest("POST", "https://pg.node.example.net/service.Service/GetSystemStats", bytes.NewReader([]byte{0, 0, 0, 0, 0}))
	req.Header.Set("content-type", "application/grpc")
	req.Header.Set("x-api-key", "fixture-key")
	req.Header.Set("te", "trailers")
	response, err := h2.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != 200 || response.ProtoMajor != 2 || response.Trailer.Get("Grpc-Status") != "0" || len(body) != 5 {
		t.Fatalf("gRPC forwarding failed: status=%d, proto=%s, trailers=%v body=%q", response.StatusCode, response.Proto, response.Trailer, body)
	}
	response, err = h1.Get("https://node.example.net/")
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(response.Body)
	response.Body.Close()
	if string(body) != "existing-site" {
		t.Fatalf("non-management SNI route changed: %q", body)
	}
}

func tlsCall(t *testing.T, client *http.Client, method, url, key, body string) int {
	t.Helper()
	req, _ := http.NewRequest(method, url, strings.NewReader(body))
	req.Header.Set("x-api-key", key)
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	io.Copy(io.Discard, response.Body)
	return response.StatusCode
}

type proxyListener struct{ net.Listener }

func (l proxyListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	header := make([]byte, 16)
	if _, err = io.ReadFull(conn, header); err != nil {
		conn.Close()
		return nil, err
	}
	if string(header[:12]) != "\r\n\r\n\x00\r\nQUIT\n" {
		conn.Close()
		return nil, fmt.Errorf("PROXY v2 header missing")
	}
	size := int64(binary.BigEndian.Uint16(header[14:16]))
	if _, err = io.CopyN(io.Discard, conn, size); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}
