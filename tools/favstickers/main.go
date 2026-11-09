// favstickers links a temporary companion device to a WhatsApp account and adds favorite stickers the way
// WhatsApp's own clients do: an app state "favoriteSticker" mutation (collection regular_low, action version 7,
// index ["favoriteSticker", <base64 sha256 of the sticker>]) carrying a StickerAction that points at a freshly
// uploaded copy of the sticker. The primary phone then downloads each sticker and adds it to its favorites.
//
//	favstickers pair <phone>          link with a pairing code (enter it in WhatsApp > Linked devices)
//	favstickers send [-dry-run] [-limit N]
//	favstickers unlink                remove this companion from the account
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const (
	favoriteStickerIndex   = "favoriteSticker"
	favoriteStickerVersion = 7
	// One favorite per patch: whatsmeow stamps every mutation of a patch with the patch's timestamp, and each
	// favorite should keep its own (the sticker tray is ordered by it).
	batchSize = 1
	doneFile  = "done.txt"
)

type favorite struct {
	File        string `json:"file"`
	Hash        string `json:"hash"`
	TimestampMs int64  `json:"timestamp_ms"`
	Mimetype    string `json:"mimetype"`
	Width       uint32 `json:"width"`
	Height      uint32 `json:"height"`
	ImageHash   string `json:"image_hash"`
	Lottie      bool   `json:"lottie"`
	Avatar      bool   `json:"avatar"`
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

func connect(ctx context.Context) *whatsmeow.Client {
	log := waLog.Stdout("wa", "WARN", true)
	container, err := sqlstore.New(ctx, "sqlite3", "file:session.db?_foreign_keys=on", log)
	if err != nil {
		fail("session store: %v", err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		fail("session device: %v", err)
	}
	return whatsmeow.NewClient(device, log)
}

func pair(ctx context.Context, phone string) {
	cli := connect(ctx)
	if cli.Store.ID != nil {
		fmt.Println("already linked as", cli.Store.ID)
		return
	}
	paired := make(chan struct{}, 1)
	keys := make(chan struct{}, 1)
	cli.AddEventHandler(func(evt any) {
		switch e := evt.(type) {
		case *events.PairSuccess:
			fmt.Println("linked as", e.ID)
			paired <- struct{}{}
		case *events.PairError:
			fmt.Println("pairing failed:", e.Error)
		case *events.AppStateSyncComplete:
			fmt.Println("synced", e.Name)
			if e.Name == appstate.WAPatchRegularLow {
				select {
				case keys <- struct{}{}:
				default:
				}
			}
		}
	})
	if err := cli.Connect(); err != nil {
		fail("connect: %v", err)
	}
	defer cli.Disconnect()
	time.Sleep(2 * time.Second)
	code, err := cli.PairPhone(ctx, phone, true, whatsmeow.PairClientChrome, "Chrome (Mac OS)")
	if err != nil {
		fail("pair: %v", err)
	}
	fmt.Println("PAIRING CODE:", code)
	select {
	case <-paired:
	case <-time.After(5 * time.Minute):
		fail("not linked within 5 minutes")
	}
	fmt.Println("waiting for the phone to share its settings sync…")
	select {
	case <-keys:
		fmt.Println("ready")
	case <-time.After(3 * time.Minute):
		fmt.Println("no regular_low sync yet; run `send` and it will fetch it")
	}
}

func loadDone() map[string]bool {
	done := map[string]bool{}
	data, _ := os.ReadFile(doneFile)
	for _, line := range strings.Split(string(data), "\n") {
		if line != "" {
			done[line] = true
		}
	}
	return done
}

func send(ctx context.Context, dryRun bool, limit int) {
	var favs []favorite
	data, err := os.ReadFile("favorites.json")
	if err != nil {
		fail("favorites.json: %v", err)
	}
	if err = json.Unmarshal(data, &favs); err != nil {
		fail("favorites.json: %v", err)
	}
	done := loadDone()
	var todo []favorite
	for _, f := range favs {
		if !done[f.Hash] {
			todo = append(todo, f)
		}
	}
	if limit > 0 && len(todo) > limit {
		todo = todo[:limit]
	}
	fmt.Printf("%d favorites, %d already sent, sending %d\n", len(favs), len(done), len(todo))
	if dryRun {
		for _, f := range todo {
			fmt.Println(" ", time.UnixMilli(f.TimestampMs).Format(time.DateTime), f.Hash, f.File)
		}
		return
	}
	cli := connect(ctx)
	if cli.Store.ID == nil {
		fail("not linked yet; run `pair` first")
	}
	if err = cli.Connect(); err != nil {
		fail("connect: %v", err)
	}
	defer cli.Disconnect()
	time.Sleep(3 * time.Second)
	if err = cli.FetchAppState(ctx, appstate.WAPatchRegularLow, false, false); err != nil {
		fail("fetch regular_low: %v", err)
	}
	if id, _ := cli.Store.AppStateKeys.GetLatestAppStateSyncKeyID(ctx); id == nil {
		fail("the phone has not shared its app state keys with this device yet; wait a minute and retry")
	}
	log, err := os.OpenFile(doneFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		fail("%v", err)
	}
	defer log.Close()
	deviceHint := uint32(cli.Store.ID.Device)
	for start := 0; start < len(todo); start += batchSize {
		end := min(start+batchSize, len(todo))
		var mutations []appstate.MutationInfo
		var latest time.Time
		for _, f := range todo[start:end] {
			plain, err := os.ReadFile(f.File)
			if err != nil {
				fail("%s: %v", f.File, err)
			}
			up, err := cli.Upload(ctx, plain, whatsmeow.MediaImage)
			if err != nil {
				fail("upload %s: %v", f.File, err)
			}
			if base64.StdEncoding.EncodeToString(up.FileSHA256) != f.Hash {
				fail("%s: uploaded hash differs from the list", f.File)
			}
			action := &waSyncAction.StickerAction{
				URL:             proto.String(up.URL),
				FileEncSHA256:   up.FileEncSHA256,
				MediaKey:        up.MediaKey,
				Mimetype:        proto.String(f.Mimetype),
				Height:          proto.Uint32(f.Height),
				Width:           proto.Uint32(f.Width),
				DirectPath:      proto.String(up.DirectPath),
				FileLength:      proto.Uint64(up.FileLength),
				IsFavorite:      proto.Bool(true),
				DeviceIDHint:    proto.Uint32(deviceHint),
				IsLottie:        proto.Bool(f.Lottie),
				IsAvatarSticker: proto.Bool(f.Avatar),
			}
			if f.ImageHash != "" {
				action.ImageHash = proto.String(f.ImageHash)
			}
			ts := time.UnixMilli(f.TimestampMs)
			if f.TimestampMs <= 0 {
				ts = time.Now()
			}
			if ts.After(latest) {
				latest = ts
			}
			mutations = append(mutations, appstate.MutationInfo{
				Index:   []string{favoriteStickerIndex, f.Hash},
				Version: favoriteStickerVersion,
				Value:   &waSyncAction.SyncActionValue{Timestamp: proto.Int64(ts.UnixMilli()), StickerAction: action},
			})
		}
		patch := appstate.PatchInfo{Timestamp: latest, Type: appstate.WAPatchRegularLow, Mutations: mutations}
		if err := cli.SendAppState(ctx, patch); err != nil {
			fail("sending favorites %d-%d: %v", start+1, end, err)
		}
		for _, f := range todo[start:end] {
			fmt.Fprintln(log, f.Hash)
		}
		if end%10 == 0 || end == len(todo) {
			fmt.Printf("sent %d/%d\n", end, len(todo))
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func unlink(ctx context.Context) {
	cli := connect(ctx)
	if cli.Store.ID == nil {
		fmt.Println("not linked")
		return
	}
	if err := cli.Connect(); err != nil {
		fail("connect: %v", err)
	}
	time.Sleep(2 * time.Second)
	if err := cli.Logout(ctx); err != nil {
		fail("logout: %v", err)
	}
	fmt.Println("unlinked")
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if len(os.Args) < 2 {
		fail("usage: favstickers pair <phone> | send [-dry-run] [-limit N] | unlink")
	}
	switch os.Args[1] {
	case "pair":
		if len(os.Args) != 3 {
			fail("usage: favstickers pair <phone in international format, digits only>")
		}
		pair(ctx, os.Args[2])
	case "send":
		fs := flag.NewFlagSet("send", flag.ExitOnError)
		dry := fs.Bool("dry-run", false, "list what would be sent")
		limit := fs.Int("limit", 0, "send at most this many")
		_ = fs.Parse(os.Args[2:])
		send(ctx, *dry, *limit)
	case "unlink":
		unlink(ctx)
	default:
		fail("unknown command %q", os.Args[1])
	}
}
