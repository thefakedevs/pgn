package main

import (
	"bytes"
	"context"
	"io"
	"os/exec"
	"regexp"
)

var ansiRegexp = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func runCommand(ctx context.Context, name string, args ...string) (string, int, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = io.Discard
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	err := cmd.Run()
	if err != nil {
		exitCode := 1
		if ee, ok := err.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		}
		return stderr.String(), exitCode, err
	}

	return "", 0, nil
}

func cleanANSI(s string) string {
	return ansiRegexp.ReplaceAllString(s, "")
}
