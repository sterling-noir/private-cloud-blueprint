// Local patch (home server, 2026-11-15): chats listed in /data/ignore-chats.txt are never bridged.
// One entry per line: a chat GUID ("any;-;+15550100") or just the address ("+15550100",
// "someone@example.com"). Lines starting with # are comments. The file is re-read when it changes.
package main

import (
	"bufio"
	"os"
	"strings"
	"sync"
	"time"
)

const ignoreChatsFile = "/data/ignore-chats.txt"

var (
	ignoreChatsLock  sync.Mutex
	ignoreChatsSet   = map[string]struct{}{}
	ignoreChatsMtime time.Time
)

func chatAddress(guid string) string {
	if i := strings.LastIndex(guid, ";-;"); i >= 0 {
		return guid[i+3:]
	}
	if i := strings.LastIndex(guid, ";+;"); i >= 0 {
		return guid[i+3:]
	}
	return guid
}

func isIgnoredChat(guid string) bool {
	if guid == "" {
		return false
	}
	ignoreChatsLock.Lock()
	defer ignoreChatsLock.Unlock()
	if st, err := os.Stat(ignoreChatsFile); err == nil {
		if !st.ModTime().Equal(ignoreChatsMtime) {
			set := map[string]struct{}{}
			if f, err := os.Open(ignoreChatsFile); err == nil {
				sc := bufio.NewScanner(f)
				for sc.Scan() {
					line := strings.TrimSpace(strings.SplitN(sc.Text(), "#", 2)[0])
					if line != "" {
						set[line] = struct{}{}
						set[chatAddress(line)] = struct{}{}
					}
				}
				f.Close()
			}
			ignoreChatsSet, ignoreChatsMtime = set, st.ModTime()
		}
	} else {
		ignoreChatsSet = map[string]struct{}{}
	}
	if _, ok := ignoreChatsSet[guid]; ok {
		return true
	}
	_, ok := ignoreChatsSet[chatAddress(guid)]
	return ok
}

// dropIgnoredPortal lets go of an existing room for an ignored chat (the bridge's own cleanup:
// ghosts leave; the room can then be deleted on the server).
func dropIgnoredPortal(portal *Portal) {
	if len(portal.MXID) == 0 {
		return
	}
	portal.log.Infoln("Chat is on the ignore list, releasing its Matrix room")
	portal.Cleanup(false)
	portal.Delete()
}
