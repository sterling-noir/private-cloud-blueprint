// Local patch (home server, 2026-11-15): chats listed in /data/ignore-chats.txt are never bridged.
// One entry per line: a chat GUID ("any;-;+15550100") or just the address ("+15550100",
// "someone@example.com"). Lines starting with # are comments. The file is re-read when it changes.
//
// 2026-11-19 (security review): the list fails CLOSED.
//   - Once a list has been loaded, a missing, unreadable or suddenly empty file keeps the last good
//     list (an editor saving the file must never switch the block off, even for a moment).
//   - If the file exists but cannot be read and no list was ever loaded, every chat is ignored.
//   - If the file has never existed, the feature is off and nothing is ignored.
//   - Matching ignores upper/lower case. To empty a list that had entries, restart the bridge.
package main

import (
	"bufio"
	"os"
	"strings"
	"sync"
	"time"
)

var ignoreChatsFile = "/data/ignore-chats.txt"

var (
	ignoreChatsLock   sync.Mutex
	ignoreChatsSet    = map[string]struct{}{}
	ignoreChatsMtime  time.Time
	ignoreChatsLoaded bool // a list was read successfully at least once
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

// readIgnoreChats returns the entries of the file, lower-cased. ok is false if it could not be read completely.
func readIgnoreChats() (set map[string]struct{}, ok bool) {
	f, err := os.Open(ignoreChatsFile)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	set = map[string]struct{}{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.ToLower(strings.TrimSpace(strings.SplitN(sc.Text(), "#", 2)[0]))
		if line != "" {
			set[line] = struct{}{}
			set[chatAddress(line)] = struct{}{}
		}
	}
	if sc.Err() != nil {
		return nil, false
	}
	return set, true
}

func isIgnoredChat(guid string) bool {
	if guid == "" {
		return false
	}
	ignoreChatsLock.Lock()
	defer ignoreChatsLock.Unlock()
	st, err := os.Stat(ignoreChatsFile)
	switch {
	case err != nil && os.IsNotExist(err) && !ignoreChatsLoaded:
		return false // no list was ever configured: the feature is off
	case err != nil && !ignoreChatsLoaded:
		return true // the file is there but unreadable and nothing is loaded: block everything
	case err != nil:
		// the file vanished or became unreadable after a good load: keep the last good list
	case !st.ModTime().Equal(ignoreChatsMtime):
		set, ok := readIgnoreChats()
		switch {
		case !ok && !ignoreChatsLoaded:
			return true
		case !ok:
			// keep the last good list and try again on the next message
		case len(set) == 0 && len(ignoreChatsSet) > 0:
			// a list with entries never becomes empty at runtime (half-written file); keep it
		default:
			ignoreChatsSet, ignoreChatsMtime, ignoreChatsLoaded = set, st.ModTime(), true
		}
	}
	g := strings.ToLower(guid)
	if _, ok := ignoreChatsSet[g]; ok {
		return true
	}
	_, ok := ignoreChatsSet[chatAddress(g)]
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
