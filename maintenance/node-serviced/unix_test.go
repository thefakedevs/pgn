package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T) (*http.Client, string, *server) {
	t.Helper()
	// Short path works within both Linux's and macOS's sockaddr_un limits.
	dir, err := os.MkdirTemp("", "pgn-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	log := filepath.Join(dir, "commands")
	app := filepath.Join(dir, "fake-pgn")
	if err := os.WriteFile(app, []byte("#!/bin/sh\nprintf '%s\\n' \"$*\" >> '"+log+"'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "api.sock")
	listener, err := listenUnix(path, os.Getgid())
	if err != nil {
		t.Fatal(err)
	}
	s := &server{cfg: config{APIKey: "fixture-key", AppName: app}}
	httpServer := &http.Server{Handler: routes(s)}
	go httpServer.Serve(listener)
	t.Cleanup(func() { httpServer.Close(); listener.Close() })
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}}
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport, Timeout: 2 * time.Second}, log, s
}

func call(t *testing.T, client *http.Client, method, path, key, body string) int {
	t.Helper()
	req, err := http.NewRequest(method, "http://unix"+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("x-api-key", key)
	r, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	io.Copy(io.Discard, r.Body)
	return r.StatusCode
}

func TestUnixAPIAuthenticationAndCommandContract(t *testing.T) {
	client, log, _ := fixture(t)
	for _, path := range []string{"/node/update", "/node/core_update", "/node/geofiles", "/node/hard_reset"} {
		for _, key := range []string{"", "wrong"} {
			if code := call(t, client, "POST", path, key, `{}`); code != 401 {
				t.Fatalf("%s unauthorized=%d", path, code)
			}
		}
	}
	if _, err := os.Stat(log); !os.IsNotExist(err) {
		t.Fatal("unauthorized request executed a command")
	}
	if code := call(t, client, "GET", "/", "fixture-key", ""); code != 200 {
		t.Fatal(code)
	}
	tests := []struct{ path, body, want string }{
		{"/node/update", `{}`, "update --no-update-service"},
		{"/node/core_update", `{"core_version":"26.11.9"}`, "core-update --version v26.11.9"},
		{"/node/geofiles", `{"region":"RUSSIA"}`, "geofiles --russia"},
		{"/node/hard_reset", `{}`, "restart"},
	}
	for _, tt := range tests {
		if code := call(t, client, "POST", tt.path, "fixture-key", tt.body); code != 200 {
			t.Fatalf("%s=%d", tt.path, code)
		}
		output, _ := os.ReadFile(log)
		if !strings.HasSuffix(string(output), tt.want+"\n") {
			t.Fatalf("unexpected command: %q", output)
		}
	}
	before, _ := os.ReadFile(log)
	for _, tt := range []struct{ path, body string }{
		{"/node/core_update", `{"core_version":"latest; touch /tmp/pgn-invalid"}`},
		{"/node/geofiles", `{"region":"russia --help"}`},
		{"/node/geofiles", `{`},
	} {
		if code := call(t, client, "POST", tt.path, "fixture-key", tt.body); code != 400 {
			t.Fatal(code)
		}
	}
	if code := call(t, client, "GET", "/unknown", "fixture-key", ""); code != 404 {
		t.Fatal(code)
	}
	after, _ := os.ReadFile(log)
	if string(before) != string(after) {
		t.Fatal("invalid input executed a command")
	}
}

func TestOverlappingUpdatesAreRejected(t *testing.T) {
	client, log, s := fixture(t)
	s.busy.Lock()
	defer s.busy.Unlock()
	if code := call(t, client, "POST", "/node/update", "fixture-key", ""); code != 409 {
		t.Fatal(code)
	}
	if _, err := os.Stat(log); !os.IsNotExist(err) {
		t.Fatal("overlapping update executed")
	}
}

func TestUnixListenerDoesNotReplaceFilesOrLiveSockets(t *testing.T) {
	dir, err := os.MkdirTemp("", "pgn-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "api.sock")
	if err := os.WriteFile(path, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := listenUnix(path, os.Getgid()); err == nil {
		t.Fatal("replaced a regular file")
	}
	os.Remove(path)
	first, err := listenUnix(path, os.Getgid())
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if _, err := listenUnix(path, os.Getgid()); err == nil {
		t.Fatal("replaced a live socket")
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0660 {
		t.Fatalf("permissions: %v", info.Mode())
	}
}
