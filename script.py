import yt_dlp
import sys
from pathlib import Path
import argparse
import threading
import queue
import re
import hashlib
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
from flask import Flask, request, jsonify, render_template, send_from_directory
import logging
from datetime import datetime
import os


@dataclass
class DownloadTask:
    url: str
    status: str
    progress: float
    speed: str
    filename: Optional[str]
    eta: Optional[str]
    size: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class DownloadManager:
    def __init__(self):
        self.download_queue = queue.Queue()
        self.active_downloads: Dict[str, DownloadTask] = {}
        self.completed_downloads: List[DownloadTask] = []
        self.max_concurrent_downloads = 3
        self.active_threads = []
        self.start_worker_threads()

    def start_worker_threads(self):
        for _ in range(self.max_concurrent_downloads):
            thread = threading.Thread(target=self._download_worker, daemon=True)
            thread.start()
            self.active_threads.append(thread)

    def _download_worker(self):
        while True:
            try:
                task = self.download_queue.get()
                self._process_download(task)
            except Exception as e:
                logging.error(f"Worker error: {str(e)}")
            finally:
                self.download_queue.task_done()

    def _process_download(self, task: DownloadTask):
        try:
            downloader = VideoDownloader(task=task)
            downloader.download_video(task.url)
        except Exception as e:
            task.error = str(e)
            task.status = "failed"
        finally:
            task.completed_at = datetime.now()
            self.completed_downloads.append(task)
            if task.url in self.active_downloads:
                del self.active_downloads[task.url]

    def add_download(self, url: str) -> str:
        if not self._validate_url(url):
            raise ValueError("Invalid YouTube URL")

        task = DownloadTask(
            url=url,
            status="queued",
            progress=0.0,
            speed="0 KB/s",
            filename=None,
            eta=None,
            size=None,
            created_at=datetime.now(),
        )

        self.active_downloads[url] = task
        self.download_queue.put(task)
        return url

    def _validate_url(self, url: str) -> bool:
        youtube_regex = r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$"
        return bool(re.match(youtube_regex, url))

    def get_download_status(self, url: str) -> Optional[DownloadTask]:
        return self.active_downloads.get(url)

    def get_all_downloads(self) -> Dict[str, List[DownloadTask]]:
        return {
            "active": list(self.active_downloads.values()),
            "completed": self.completed_downloads[-10:],  # Last 10 completed downloads
        }


class VideoDownloader:
    def __init__(self, output_dir="downloads", task: Optional[DownloadTask] = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.task = task

        self.ydl_opts = {
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "outtmpl": str(self.output_dir / "%(title)s.%(ext)s"),
            "progress_hooks": [self._progress_hook],
            "quiet": False,
            "no_warnings": False,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt",
            "ratelimit": 1000000,  # 1MB/s
            "postprocessors": [
                {
                    "key": "FFmpegMetadata",
                }
            ],
        }

    def _progress_hook(self, d):
        if not self.task:
            return

        if d["status"] == "downloading":
            # Update download progress
            if total_bytes := d.get("total_bytes"):
                downloaded = d.get("downloaded_bytes", 0)
                self.task.progress = (downloaded / total_bytes) * 100
                self.task.speed = d.get("speed", 0)
                self.task.eta = d.get("eta", 0)
                self.task.size = self._format_bytes(total_bytes)
                self.task.status = "downloading"
                self.task.filename = d.get("filename")

        elif d["status"] == "finished":
            self.task.status = "processing"
            self.task.progress = 100.0

    def _format_bytes(self, bytes_num: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_num < 1024:
                return f"{bytes_num:.1f} {unit}"
            bytes_num /= 1024
        return f"{bytes_num:.1f} TB"

    def download_video(
        self,
        url: str,
        resolution="1080",
        audio_only=False,
        video_only=False,
        playlist=False,
        subtitle_langs=None,
    ):
        try:
            # Configure format based on options
            if audio_only:
                self.ydl_opts["format"] = "bestaudio/best"
                self.ydl_opts["postprocessors"].append(
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                )
            elif video_only:
                self.ydl_opts["format"] = f"bestvideo[height<={resolution}]"
            else:
                self.ydl_opts["format"] = (
                    f"bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]"
                )

            # Configure playlist options
            self.ydl_opts["noplaylist"] = not playlist

            # Configure subtitles
            if subtitle_langs:
                self.ydl_opts["subtitleslangs"] = subtitle_langs

            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                # Calculate hash of the video ID for verification
                video_id = info.get("id", "")
                hash_obj = hashlib.md5(video_id.encode())
                expected_hash = hash_obj.hexdigest()

                # Start download
                if self.task:
                    self.task.status = "downloading"
                ydl.download([url])

                # Verify download
                if self.task and self.task.filename:
                    file_hash = self._calculate_file_hash(self.task.filename)
                    if file_hash != expected_hash:
                        raise Exception("File verification failed")

                return True

        except Exception as e:
            if self.task:
                self.task.status = "failed"
                self.task.error = str(e)
            raise

    def _calculate_file_hash(self, filename: str) -> str:
        hash_obj = hashlib.md5()
        with open(filename, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_obj.update(chunk)
        return hash_obj.hexdigest()


# Flask Application
app = Flask(__name__, template_folder="views")
download_manager = DownloadManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    try:
        data = request.get_json()
        url = data["url"]
        task_id = download_manager.add_download(url)
        return jsonify({"status": "success", "task_id": task_id})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/status/<task_id>")
def get_status(task_id):
    task = download_manager.get_download_status(task_id)
    if task:
        return jsonify(
            {
                "status": task.status,
                "progress": task.progress,
                "speed": task.speed,
                "eta": task.eta,
                "filename": task.filename,
            }
        )
    return jsonify({"status": "not_found"}), 404


@app.route("/api/downloads")
def get_downloads():
    downloads = download_manager.get_all_downloads()
    return jsonify(downloads)


@app.route("/downloads/<path:filename>")
def download_file(filename):
    return send_from_directory("downloads", filename)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host="0.0.0.0", port=5000)

    print("Current Working Directory:", os.getcwd())
    print("Template Folder:", app.template_folder)
