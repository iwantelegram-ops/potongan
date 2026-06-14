"""
antigcast.py — Entry Point Bot Antispam + Nexus AI
Jalankan: python antigcast.py

Sistem yang berjalan:
  [REFACTOR] plugins/filters/    → antispam, bio, cas  (group filter)
  [REFACTOR] plugins/commands/   → settings, regex, free, log, antigcast_group
  [REFACTOR] plugins/ui/         → DM panel interaktif (pages, handlers_dm, handlers_fsm)
  [NEXUS]    plugins/nexus/      → nexus_group.py, nexus_handlers.py
             core/               → engine.py (komputasi AI)

Database (otomatis dipilih saat startup):
  1. MongoDB  — jika MONGO_URL ada di .env dan bisa tersambung
  2. SQLite   — fallback ke penyimpanan internal HP (Termux)
"""

import os
import sys
import asyncio
import threading
from pathlib import Path as _Path
import dns.resolver
from pyrogram import Client, idle
from pyrogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Path fix: pastikan semua import lokal bisa ditemukan dari CWD manapun ─────
# _BOT_DIR adalah folder tempat antigcast.py berada (misal: /sdcard/bot-main/).
# sys.path.insert memastikan Python selalu menemukan modules lokal (database,
# plugins/, core/, dll) meskipun script dijalankan dari direktori lain.
_BOT_DIR = _Path(__file__).resolve().parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from database import setup_db, delete_worker, close_db, get_bot_config, save_bot_config, get_active_backend
from admin_session import start_cleanup_task as _adm_cleanup

# ── Termux: ambil OWNER_ID ────────────────────────────────────────────────────
OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# ── Fix DNS Termux ────────────────────────────────────────────────────────────
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['223.5.5.5', '223.6.6.6']

# ── Env ───────────────────────────────────────────────────────────────────────
API_ID    = int(os.environ.get("API_ID", 0))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CODE_BOT  = os.environ.get("CODE_BOT", "").strip()

# ── Session name — berbasis CODE_BOT jika tersedia, fallback ke bot_id ────────
# Jika CODE_BOT diset:
#   • Semua bot dengan CODE_BOT yang sama berbagi satu file session.
#   • Ganti BOT_TOKEN → session lama tetap dipakai, pengaturan grup tidak reset.
# Jika CODE_BOT kosong:
#   • Fallback ke bot_id dari token (perilaku lama) agar tidak patah.
_BOT_ID = BOT_TOKEN.split(":")[0] if ":" in BOT_TOKEN else "default"

# ── Session suffix: selalu berbasis CODE_BOT + BOT_ID ─────────────────────────
# Tujuan: 2 bot clone (CODE_BOT sama, BOT_TOKEN beda) bisa jalan bersamaan
# tanpa berebut file session. Data grup/regex/dll tetap berbagi lewat CODE_BOT.
# Contoh:
#   Bot 1: CODE_BOT=produksi, BOT_ID=111 → session: antispam_bot_produksi_111
#   Bot 2: CODE_BOT=produksi, BOT_ID=222 → session: antispam_bot_produksi_222
#   Keduanya baca/tulis database namespace "produksi" yang sama.
_SESSION_SUFFIX = f"{CODE_BOT}_{_BOT_ID}" if CODE_BOT else f"token_{_BOT_ID}"
_SESSION_NAME = str(_BOT_DIR / f"antispam_bot_{_SESSION_SUFFIX}")


def _print_startup_banner():
    """Tampilkan banner info bot saat startup di Termux."""
    print(f"\n")
    print(f"{'  BOT ANTISPAM + NEXUS AI  ':^52}")

    token_display = (BOT_TOKEN[:8] + "…" + BOT_TOKEN[-4:]) if len(BOT_TOKEN) > 12 else "(tidak diset)"
    sess_display  = f"antispam_bot_{_SESSION_SUFFIX}.session"
    print(f"  API_ID    : {str(API_ID) if API_ID else '(tidak diset)':<39}")
    print(f"  BOT_TOKEN : {token_display:<39}")
    print(f"  BOT_ID    : {_BOT_ID:<39}")
    print(f"  Session   : {sess_display:<39}")
    print(f"  OWNER_ID  : {str(OWNER_ID) if OWNER_ID else '(tidak diset)':<39}")
    if CODE_BOT:
        print(f"  CODE_BOT  : [{CODE_BOT}]{'':>{39 - len(CODE_BOT) - 2}}")
        print(f"  Namespace : aktif — data & session berbagi per CODE_BOT")
    else:
        print(f"  CODE_BOT  : (kosong — tidak ada isolasi)        ")
        print(f"  ⚠️  Set CODE_BOT di .env agar data tidak campur ")

    print(f"  Info backend database menyusul di bawah...      ")
    print(f"\n")

# ── Client ────────────────────────────────────────────────────────────────────
# Session name = path absolut + bot_id suffix.
# Tiap BOT_TOKEN punya file .session sendiri → tidak pernah bentrok.
# plugins root tetap "plugins" (nama modul Python, bukan path filesystem) —
# Python sudah tahu mencarinya lewat sys.path yang sudah diset di atas.
_SESSION_DB_KEY = f"pyrogram_session_{_SESSION_SUFFIX}"

app: Client = None  # diinisialisasi di _build_client() dalam main()


async def _build_client() -> Client:
    """
    Buat Pyrogram Client pakai file session lokal seperti biasa.
    Setelah login, file session disimpan ke MongoDB sebagai backup.
    """
    client = Client(
        _SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        plugins=dict(root="plugins"),
    )
    return client


async def _restore_session_from_mongo() -> bool:
    """
    Pulihkan file .session dari MongoDB jika file lokal tidak ada.
    Hanya restore jika file lokal TIDAK ADA (misal setelah Railway redeploy).
    Jika BOT_TOKEN berubah sejak session terakhir disimpan → hapus session lama
    dan biarkan bot login ulang dengan token baru.
    """
    import base64, os as _os

    if get_active_backend() != "mongo":
        return False

    session_path = _SESSION_NAME + ".session"
    if _os.path.exists(session_path):
        return False  # File lokal ada, tidak perlu restore

    # ── Cek apakah BOT_TOKEN berubah sejak session terakhir disimpan ──────────
    _TOKEN_DB_KEY = f"last_bot_token_{_SESSION_SUFFIX}"
    saved_token = await get_bot_config(_TOKEN_DB_KEY)
    if saved_token and saved_token != BOT_TOKEN:
        print(f"[Session] ⚠️  BOT_TOKEN berubah — session lama dihapus, bot login ulang.")
        await save_bot_config(_SESSION_DB_KEY, None)
        await save_bot_config(_TOKEN_DB_KEY, None)
        return False

    saved_bytes = await get_bot_config(_SESSION_DB_KEY)
    if not saved_bytes:
        print(f"[Session] ℹ️  Belum ada session di MongoDB, bot akan login baru.")
        return False

    try:
        raw = base64.b64decode(saved_bytes.encode())
        with open(session_path, "wb") as _f:
            _f.write(raw)
        print(f"[Session] ✅ File session dipulihkan dari MongoDB.")
        return True
    except Exception as e:
        print(f"[Session] ⚠️  Gagal pulihkan session: {e}")
        return False


async def _clear_session_from_mongo() -> None:
    """Hapus session dari MongoDB — dipanggil jika session yang dipulihkan ditolak Telegram."""
    try:
        await save_bot_config(_SESSION_DB_KEY, None)
        print(f"[Session] 🗑️  Session lama dihapus dari MongoDB.")
    except Exception as e:
        print(f"[Session] ⚠️  Gagal hapus session dari MongoDB: {e}")


async def _save_session_to_mongo() -> None:
    """
    Baca file .session dari disk dan simpan isinya (base64) ke MongoDB.
    Dipanggil setelah app.start() berhasil — MongoDB selalu diupdate dari file lokal.
    Juga menyimpan BOT_TOKEN aktif agar saat redeploy bisa deteksi token berubah.
    """
    import base64, os as _os

    if get_active_backend() != "mongo":
        return
    try:
        session_path = _SESSION_NAME + ".session"
        if not _os.path.exists(session_path):
            return
        with open(session_path, "rb") as _f:
            raw = _f.read()
        encoded = base64.b64encode(raw).decode()
        await save_bot_config(_SESSION_DB_KEY, encoded)
        # Simpan token aktif untuk deteksi perubahan di deploy berikutnya
        _TOKEN_DB_KEY = f"last_bot_token_{_SESSION_SUFFIX}"
        await save_bot_config(_TOKEN_DB_KEY, BOT_TOKEN)
        print(f"[Session] ✅ Session disimpan ke MongoDB.")
    except Exception as e:
        print(f"[Session] ⚠️  Gagal simpan session ke MongoDB: {e}")

# ── Health Check ──────────────────────────────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Antispam + Nexus AI Online 2026")

    def log_message(self, *args):
        pass


def run_health_check():
    try:
        port = int(os.environ.get("PORT", 8000))
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        print(f"[HealthCheck] Error: {e}")


# ── Set Bot Commands ──────────────────────────────────────────────────────────
async def _setup_commands():
    try:
        await app.set_bot_commands(
            commands=[
                BotCommand("spam",      "balas pesan n masukin ke database AI"),
                BotCommand("antigcast", "anti spam cerdas"),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
        await app.set_bot_commands(
            commands=[
                BotCommand("antigcast", "anti spam cerdas"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        print("✅ Bot commands berhasil diset (grup & DM).")
    except Exception as e:
        print(f"⚠️  Gagal set bot commands: {e}")


# ── Resolve Channel Peer ──────────────────────────────────────────────────────
async def _resolve_channel_peer(client):
    """
    Resolve CHANNEL_OWNER dari .env ke Telegram peer, lalu simpan info-nya
    (title + username) ke database cloud.

    Tujuan:
      • Sesi baru (ganti token) belum pernah "melihat" channel → PeerIdInvalid
      • Dengan menyimpan title + username ke DB, _fetch_owner_line() bisa
        menampilkan nama channel di /start meski get_chat() gagal di sesi baru
      • Username di DB memungkinkan resolve ulang via @username saat bot restart

    Dipanggil sekali setelah app.start() di main().
    """
    from database import save_bot_config
    ch_id = int(os.environ.get("CHANNEL_OWNER", 0))
    if not ch_id:
        return
    # Coba pulihkan access_hash dari peer cache MongoDB dulu (lihat
    # _restore_peer_from_db) — mengurangi PeerIdInvalid pada sesi baru.
    await _restore_peer_from_db(client, ch_id)
    try:
        ch = await client.get_chat(ch_id)
        title    = ch.title or ""
        username = getattr(ch, "username", None) or ""
        await save_bot_config("channel_owner_id",       ch_id)
        await save_bot_config("channel_owner_title",    title)
        await save_bot_config("channel_owner_username", username)
        label = f"@{username}" if username else f"(no username, id={ch_id})"
        print(f"[Startup] ✅ CHANNEL_OWNER '{title}' {label} berhasil di-cache ke DB.")
    except Exception as e:
        print(f"[Startup] ⚠️  Gagal resolve CHANNEL_OWNER ({ch_id}): {e}")
        print(f"           Info channel akan diambil dari cache DB (jika sudah pernah disimpan sebelumnya).")


# ── Broadcast "Bot Started" ───────────────────────────────────────────────────
_STARTUP_MSG_DELETE_AFTER = 10   # detik sebelum pesan startup dihapus
_STARTUP_MSG_DELAY        = 1.5  # delay antar pengiriman (anti FloodWait)

# ── Peer cache persistence ─────────────────────────────────────────────────────
# Pyrogram (bot account) hanya bisa resolve send_message/get_chat_member ke
# sebuah chat jika access_hash chat tersebut sudah ada di cache storage lokal
# (client.storage). Cache ini biasanya terisi dari UPDATE yang masuk (pesan,
# member join, dll). Setelah Railway redeploy → container baru → file session
# baru/dipulihkan TANPA cache peer untuk chat lama → PeerIdInvalid, walau bot
# masih admin di sana.
#
# Solusi: setiap kali ada update masuk dari sebuah chat (RawUpdateHandler di
# main()), simpan (id, access_hash, type) chat tersebut ke MongoDB collection
# "peer_cache" (terpisah dari bot_config — agar config penting lain tidak
# numpuk meski jumlah grup banyak). Saat startup berikutnya, sebelum
# broadcast, peer yang tersimpan di-inject kembali ke client.storage via
# update_peers() — efeknya identik dengan "baru menerima pesan dari chat itu",
# tanpa perlu pesan asli.


async def _save_peer_to_db(client, chat_id: int) -> None:
    """
    Simpan access_hash chat_id ke MongoDB jika peer sudah dikenal Pyrogram.

    Memakai client.resolve_peer() (API publik, toleran format bot-API
    -100xxxxxxxxxx) — jika peer sudah ada di cache storage, ini langsung
    mengembalikan InputPeerChannel/InputPeerChat/InputPeerUser tanpa request
    ke server. Jika belum dikenal, akan raise — di sini kita abaikan saja
    (memang belum bisa disimpan).
    """
    try:
        from pyrogram.raw.types import InputPeerChannel, InputPeerChat, InputPeerUser
        peer = await client.resolve_peer(chat_id)
        if isinstance(peer, InputPeerChannel):
            access_hash, peer_type = peer.access_hash, "channel"
        elif isinstance(peer, InputPeerChat):
            access_hash, peer_type = 0, "chat"  # basic group, tidak pakai access_hash
        elif isinstance(peer, InputPeerUser):
            access_hash, peer_type = peer.access_hash, "user"
        else:
            return
        from database import db as _db
        await _db["peer_cache"].update_one(
            {"_id": str(chat_id)},
            {"$set": {"id": chat_id, "access_hash": access_hash, "type": peer_type}},
            upsert=True,
        )
    except Exception:
        pass  # belum dikenal / gagal — abaikan diam-diam


async def _restore_peer_from_db(client, chat_id: int) -> bool:
    """
    Cek apakah chat_id sudah dikenal di cache Pyrogram (resolve_peer berhasil
    tanpa request server). Jika tidak, coba pulihkan dari MongoDB (disimpan
    via _save_peer_to_db sebelumnya) dengan storage.update_peers().
    Return True jika peer sudah/berhasil dikenal secara lokal.
    """
    try:
        await client.resolve_peer(chat_id)
        return True  # sudah dikenal, tidak perlu restore
    except Exception:
        pass

    try:
        from database import db as _db
        cached = await _db["peer_cache"].find_one({"_id": str(chat_id)})
        if not cached or "access_hash" not in cached:
            return False

        # Konversi id bot-API (-100xxxxxxxxxx / -xxxxxxxxxx) → id "bare" yang
        # dipakai tabel peers internal storage.
        raw_id  = chat_id
        ptype   = str(cached.get("type") or "")
        if ptype == "channel" and chat_id < 0:
            raw_id = int(str(chat_id)[4:]) if str(chat_id).startswith("-100") else abs(chat_id)
        elif chat_id < 0:
            raw_id = abs(chat_id)

        await client.storage.update_peers(
            [(raw_id, int(cached["access_hash"]), ptype, None, None)]
        )
        # Verifikasi: resolve_peer harus berhasil sekarang
        await client.resolve_peer(chat_id)
        return True
    except Exception as e:
        print(f"[Startup] ⚠️  Gagal pulihkan peer {chat_id} dari MongoDB: {e}")
        return False


async def _on_any_update(client, update, users, chats) -> None:
    """
    RawUpdateHandler global — dipasang di main() setelah app.start().
    Setiap update yang masuk (pesan, member update, dsb) membawa info chat/user
    yang membuat Pyrogram mengisi cache peer-nya sendiri. Di sini kita hanya
    menyalin entri tersebut ke MongoDB agar bisa dipulihkan setelah redeploy.
    Non-blocking, gagal diam-diam — tidak boleh mengganggu handler lain.

    `chats` berisi raw types.Chat/types.Channel dengan id "bare" (positif).
    Dikonversi ke format bot-API (-100xxxxxxxxxx untuk channel/supergroup)
    via pyrogram.utils.get_peer_id agar konsisten dengan chat_id yang
    dipakai di config_db/seluruh kode bot.
    """
    try:
        if not chats:
            return
        from pyrogram import utils as _putils
        for raw_id, raw_chat in chats.items():
            try:
                chat_id = _putils.get_peer_id(raw_chat)
            except Exception:
                chat_id = raw_id
            await _save_peer_to_db(client, chat_id)
    except Exception:
        pass


async def _send_and_autodelete(client, chat_id: int, text: str) -> bool:
    """
    Kirim pesan ke chat_id, lalu hapus otomatis setelah _STARTUP_MSG_DELETE_AFTER detik.
    Penghapusan dijalankan sebagai task terpisah agar tidak memblokir broadcast
    ke chat lain. Menangani FloodWait dengan retry sekali.

    Sebelum kirim:
      1. _restore_peer_from_db() — coba pulihkan access_hash dari MongoDB
         (disimpan dari sesi sebelumnya via _on_any_update).
      2. get_chat_member(chat_id, "me") — fallback resolve via server jika
         access_hash belum/tidak ada.

    Return True jika pesan berhasil terkirim.
    """
    from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid, ChatIdInvalid, ChatWriteForbidden

    async def _delayed_delete(msg):
        try:
            await asyncio.sleep(_STARTUP_MSG_DELETE_AFTER)
            await msg.delete()
        except Exception:
            pass

    # ── 1. Coba pulihkan peer dari cache MongoDB ─────────────────────────────
    await _restore_peer_from_db(client, chat_id)

    # ── 2. Resolve via server jika masih belum dikenal ───────────────────────
    try:
        await client.get_chat_member(chat_id, "me")
    except (PeerIdInvalid, ChannelInvalid, ChatIdInvalid) as e:
        print(f"[Startup] ⚠️  Peer {chat_id} tidak dikenal sesi ini, skip: {e}")
        return False
    except Exception:
        pass  # error lain (mis. UserNotParticipant) diabaikan, coba kirim langsung

    try:
        msg = await client.send_message(chat_id, text, disable_notification=True)
        asyncio.create_task(_delayed_delete(msg))
        # Berhasil kirim → simpan/refresh peer cache untuk redeploy berikutnya
        asyncio.create_task(_save_peer_to_db(client, chat_id))
        return True
    except FloodWait as e:
        wait_s = e.value + 1
        print(f"[Startup] ⏳ FloodWait {wait_s}s saat kirim ke {chat_id}, menunggu...")
        try:
            await asyncio.sleep(wait_s)
            msg = await client.send_message(chat_id, text, disable_notification=True)
            asyncio.create_task(_delayed_delete(msg))
            asyncio.create_task(_save_peer_to_db(client, chat_id))
            return True
        except Exception as e2:
            print(f"[Startup] ⚠️  Gagal kirim ke {chat_id} setelah retry: {e2}")
            return False
    except (PeerIdInvalid, ChannelInvalid, ChatIdInvalid, ChatWriteForbidden):
        # Chat tidak bisa diakses lagi (bot dikeluarkan / channel invalid) — abaikan diam-diam
        return False
    except Exception as e:
        print(f"[Startup] ⚠️  Gagal kirim ke {chat_id}: {e}")
        return False


async def broadcast_startup_message(client) -> None:
    """
    Kirim pesan "🤖 Bot started..." ke:
      1. LOG_CHANNEL (.env)
      2. CHANNEL_OWNER (.env)
      3. Semua grup yang tersimpan di config_db (status)

    Pesan otomatis dihapus setelah _STARTUP_MSG_DELETE_AFTER detik.
    Tujuan: bot "mengenali" ulang semua chat (resolve peer cache) setiap
    startup/redeploy tanpa meninggalkan jejak permanen, dengan jeda antar
    kirim untuk menghindari FloodWait.

    Dijalankan sebagai background task — tidak memblokir startup utama.
    """
    from database import config_db

    text = "🤖 Bot started..."

    targets: list[int] = []

    log_channel = int(os.environ.get("LOG_CHANNEL", 0))
    if log_channel:
        targets.append(log_channel)

    channel_owner = int(os.environ.get("CHANNEL_OWNER", 0))
    if channel_owner and channel_owner != log_channel:
        targets.append(channel_owner)

    # Semua grup tersimpan (DEFAULT_CONFIG → koleksi "status")
    try:
        async for doc in config_db.find({}):
            cid = doc.get("chat_id")
            if isinstance(cid, int) and cid not in targets:
                targets.append(cid)
    except Exception as e:
        print(f"[Startup] ⚠️  Gagal ambil daftar grup: {e}")

    if not targets:
        return

    print(f"[Startup] 📢 Mengirim pesan startup ke {len(targets)} chat...")

    sent = 0
    for cid in targets:
        ok = await _send_and_autodelete(client, cid, text)
        if ok:
            sent += 1
        await asyncio.sleep(_STARTUP_MSG_DELAY)

    print(f"[Startup] ✅ Pesan startup terkirim ke {sent}/{len(targets)} chat "
          f"(akan terhapus otomatis dalam {_STARTUP_MSG_DELETE_AFTER}s).")


# ── Graceful Shutdown ─────────────────────────────────────────────────────────
async def _notify_owner():
    """Kirim notif ke owner lalu return. Dibatasi timeout 8 detik."""
    if not OWNER_ID:
        return
    try:
        await asyncio.wait_for(
            app.send_message(OWNER_ID, "⚠️ Bot offline — shutdown/maintenance."),
            timeout=8.0,
        )
        print("📢 Notifikasi shutdown terkirim ke owner.")
    except Exception as e:
        print(f"[Shutdown] Gagal kirim notif owner: {e}")


async def graceful_shutdown():
    """
    Tutup bot dengan bersih. Urutan:
      1. Kirim notif ke owner (timeout 8 detik)
      2. Cancel semua background task
      3. Tutup koneksi database
      4. Stop Pyrogram (timeout 5 detik)
    """
    print("\n🛑 Memulai prosedur shutdown...")

    await _notify_owner()

    current = asyncio.current_task()
    tasks   = [t for t in asyncio.all_tasks() if t is not current]
    if tasks:
        print(f"🔄 Membatalkan {len(tasks)} background task...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("✅ Semua task dibatalkan.")

    await close_db()

    try:
        if app.is_connected:
            await asyncio.wait_for(app.stop(), timeout=5.0)
            print("✅ Koneksi Telegram berhasil diputus.")
    except asyncio.TimeoutError:
        print("⚠️  app.stop() timeout — paksa keluar.")
    except Exception as e:
        print(f"[Shutdown] app.stop error (diabaikan): {e}")

    print("🛑 Bot berhasil dimatikan dengan bersih.")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global app

    # Banner startup — tampil sebelum apapun
    _print_startup_banner()

    # Health check thread (daemon)
    threading.Thread(target=run_health_check, daemon=True).start()

    # Setup database (auto-pilih MongoDB atau SQLite)
    await setup_db()

    # Pulihkan session dari MongoDB jika file lokal tidak ada (misal setelah Railway redeploy)
    await _restore_session_from_mongo()

    # Bangun Client
    app = await _build_client()

    # Admin session cleanup — hapus sesi kedaluwarsa setiap 10 menit
    asyncio.create_task(_adm_cleanup())

    # Nexus midnight scheduler
    from plugins.nexus.engine import cron_midnight_scheduler
    asyncio.create_task(cron_midnight_scheduler())

    # Jalankan bot
    try:
        await app.start()
    except Exception as _start_err:
        # Jika session yang dipulihkan dari MongoDB ditolak Telegram → hapus dan login fresh
        if "AUTH_KEY_DUPLICATED" in str(_start_err) or "AUTH_KEY_UNREGISTERED" in str(_start_err):
            print(f"[Session] ⚠️  Session dari MongoDB tidak valid ({type(_start_err).__name__}), hapus dan login ulang...")
            import os as _os
            session_path = _SESSION_NAME + ".session"
            if _os.path.exists(session_path):
                _os.remove(session_path)
            await _clear_session_from_mongo()
            # Buat client baru tanpa session lama
            app = Client(
                _SESSION_NAME,
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=BOT_TOKEN,
                plugins=dict(root="plugins"),
            )
            await app.start()
        else:
            raise

    # Background task delete_worker dijalankan SETELAH app.start() agar client
    # sudah terkoneksi saat worker pertama kali mencoba menghapus pesan.
    asyncio.create_task(delete_worker(app))

    # Pasang RawUpdateHandler global — setiap update masuk dari chat manapun
    # akan menyalin access_hash peer chat tersebut ke MongoDB (peer cache),
    # agar saat redeploy berikutnya bot bisa langsung kirim pesan tanpa
    # PeerIdInvalid (lihat _save_peer_to_db / _restore_peer_from_db).
    try:
        from pyrogram.handlers import RawUpdateHandler
        app.add_handler(RawUpdateHandler(_on_any_update))
    except Exception as e:
        print(f"[Startup] ⚠️  Gagal pasang RawUpdateHandler (peer cache): {e}")

    try:
        # Simpan session lokal ke MongoDB setelah login berhasil
        await _save_session_to_mongo()
        await _setup_commands()
        # Resolve CHANNEL_OWNER peer → simpan ke DB agar dikenal sesi baru
        await _resolve_channel_peer(app)
        # Broadcast "Bot started..." ke log channel, owner channel, & semua grup
        # (background task, tidak memblokir startup; pesan auto-hapus)
        asyncio.create_task(broadcast_startup_message(app))
        print("🚀 Bot Antispam + Nexus AI aktif! Tekan Ctrl+C untuk berhenti.")
        await idle()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await graceful_shutdown()
    finally:
        try:
            if app.is_connected:
                await app.stop()
        except Exception:
            pass

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        # 1. Ambil semua task yang masih menggantung/pending
        pending_tasks = asyncio.all_tasks(loop)

        # 2. Batalkan semua task tersebut
        for task in pending_tasks:
            task.cancel()

        # 3. Berikan waktu sejenak agar sistem memproses pembatalan task
        if pending_tasks:
            try:
                loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))
            except Exception:
                pass

        # 4. Baru setelah itu tutup loop dengan aman
        try:
            loop.close()
        except Exception:
            pass

        print("🛑 Bot berhasil dimatikan dengan bersih.")

