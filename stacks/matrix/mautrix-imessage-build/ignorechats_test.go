package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func resetIgnoreChats(t *testing.T) string {
	t.Helper()
	ignoreChatsFile = filepath.Join(t.TempDir(), "ignore-chats.txt")
	ignoreChatsSet = map[string]struct{}{}
	ignoreChatsMtime = time.Time{}
	ignoreChatsLoaded = false
	return ignoreChatsFile
}

func writeList(t *testing.T, path, content string, age time.Duration) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	mt := time.Now().Add(age)
	if err := os.Chtimes(path, mt, mt); err != nil {
		t.Fatal(err)
	}
}

const work, other = "any;-;work@example.com", "any;-;friend@example.com"

func TestIgnoreChatsNoFileMeansFeatureOff(t *testing.T) {
	resetIgnoreChats(t)
	if isIgnoredChat(work) {
		t.Fatal("no list configured: nothing should be ignored")
	}
}

func TestIgnoreChatsBasicAndCase(t *testing.T) {
	p := resetIgnoreChats(t)
	writeList(t, p, "# work\nwork@example.com\nBoss@EXAMPLE.com # comment\n", -3*time.Hour)
	if !isIgnoredChat(work) || isIgnoredChat(other) {
		t.Fatal("listed chat must be ignored, unlisted chat must not")
	}
	if !isIgnoredChat("iMessage;-;boss@example.COM") {
		t.Fatal("matching must ignore case")
	}
}

func TestIgnoreChatsKeepsListWhenFileVanishes(t *testing.T) {
	p := resetIgnoreChats(t)
	writeList(t, p, "work@example.com\n", -3*time.Hour)
	if !isIgnoredChat(work) {
		t.Fatal("setup")
	}
	os.Remove(p)
	if !isIgnoredChat(work) {
		t.Fatal("file removed: the last good list must stay in force")
	}
	if isIgnoredChat(other) {
		t.Fatal("file removed: unlisted chats must still bridge")
	}
}

func TestIgnoreChatsKeepsListWhenFileIsTruncated(t *testing.T) {
	p := resetIgnoreChats(t)
	writeList(t, p, "work@example.com\n", -3*time.Hour)
	if !isIgnoredChat(work) {
		t.Fatal("setup")
	}
	writeList(t, p, "", -2*time.Hour) // editor truncated the file before writing it again
	if !isIgnoredChat(work) {
		t.Fatal("empty file: the last good list must stay in force")
	}
	writeList(t, p, "work@example.com\nfriend@example.com\n", -1*time.Hour) // the finished save is picked up
	if !isIgnoredChat(work) || !isIgnoredChat(other) {
		t.Fatal("a completed save must load")
	}
}

func TestIgnoreChatsUnreadableFile(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root can read any file")
	}
	p := resetIgnoreChats(t)
	writeList(t, p, "work@example.com\n", -3*time.Hour)
	os.Chmod(p, 0)
	if !isIgnoredChat(other) {
		t.Fatal("list present but unreadable and never loaded: everything must be ignored")
	}
	os.Chmod(p, 0o644)
	if !isIgnoredChat(work) || isIgnoredChat(other) {
		t.Fatal("once readable, the list applies normally")
	}
	os.Chmod(p, 0)
	writeListTouch(t, p)
	if !isIgnoredChat(work) || isIgnoredChat(other) {
		t.Fatal("unreadable after a good load: keep the last good list")
	}
}

func writeListTouch(t *testing.T, path string) {
	t.Helper()
	mt := time.Now()
	if err := os.Chtimes(path, mt, mt); err != nil {
		t.Fatal(err)
	}
}
