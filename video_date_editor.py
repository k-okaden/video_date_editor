#!/usr/bin/env python3
"""動画の撮影日時エディタ — ExifTool を使って MP4/MOV の撮影日時を GUI で編集します。

必要なもの:
  - Python 3.8 以上（tkinter が使えること）
  - ExifTool（https://exiftool.org/）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".3g2"}          # 書き換え対象
SOURCE_EXTS = VIDEO_EXTS | {".mts", ".m2ts", ".avi", ".mxf"}             # 元動画として読めるもの
DATE_TAGS = [
    "QuickTime:CreateDate", "QuickTime:ModifyDate",
    "QuickTime:TrackCreateDate", "QuickTime:TrackModifyDate",
    "QuickTime:MediaCreateDate", "QuickTime:MediaModifyDate",
]
KEYS_TAG = "Keys:CreationDate"  # iPhone 動画などが持つタイムゾーン付き撮影日時
DISPLAY_FMT = "%Y-%m-%d %H:%M:%S"
FILENAME_RE = re.compile(
    r"((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_ T]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})"
)
EXIF_DT_RE = re.compile(
    r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*([+-]\d{2}:\d{2}|Z)?"
)
TZ_RE = re.compile(r"^[+-]\d{2}:\d{2}$")


# ---------------------------------------------------------------- ExifTool

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_exiftool():
    for name in ("exiftool.exe", "exiftool(-k).exe", "exiftool"):
        p = os.path.join(app_dir(), name)
        if os.path.isfile(p):
            return p
    # .app として起動すると PATH が通らないため、Mac の標準的な場所も探す
    for p in ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"):
        if os.path.isfile(p):
            return p
    return shutil.which("exiftool")


def run_exiftool(exe, args):
    """引数を UTF-8 の引数ファイル経由で渡す（日本語ファイル名対策）。"""
    fd, argfile = tempfile.mkstemp(suffix=".args")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(args))
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.run(
            [exe, "-charset", "filename=utf8", "-@", argfile],
            capture_output=True, stdin=subprocess.DEVNULL, creationflags=flags,
        )
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    finally:
        os.remove(argfile)


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def exiftool_json(exe, args):
    _, out, _ = run_exiftool(exe, ["-j"] + args)
    try:
        return json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return []


def get_duration(d):
    """exiftool の JSON から長さ（秒）を取り出す。"""
    for k, v in d.items():
        if k.split(":")[-1] == "Duration":
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def read_dates(exe, paths):
    """{正規化パス: (CreateDate文字列, Keys:CreationDate文字列, 長さ秒)}"""
    if not paths:
        return {}
    data = exiftool_json(exe, ["-G1", "-api", "QuickTimeUTC", "-d", DISPLAY_FMT,
                               "-QuickTime:CreateDate", "-" + KEYS_TAG, "-Duration#"] + list(paths))
    return {norm(d.get("SourceFile", "")): (d.get("QuickTime:CreateDate"), d.get(KEYS_TAG),
                                            get_duration(d))
            for d in data}


def parse_exif_dt(s):
    """'2024:05:01 12:00:00+09:00' → (datetime, '+09:00' or None)。未設定値なら None。"""
    m = EXIF_DT_RE.search(str(s or ""))
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()[:6]))
    except ValueError:
        return None
    if dt.year <= 1904:  # QuickTime の未設定値
        return None
    tz = m.group(7)
    return dt, ("+00:00" if tz == "Z" else tz)


def tz_delta(tz):
    sign = -1 if tz.startswith("-") else 1
    h, m = tz[1:].split(":")
    return sign * timedelta(hours=int(h), minutes=int(m))


def sony_sidecar_date(path):
    """Sony の MXF（C0001.MXF）に付く C0001M01.XML からタイムゾーン付きの撮影日時を読む。"""
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0].lower()
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return None
    for f in names:
        if not re.fullmatch(re.escape(stem) + r"m\d\d\.xml", f.lower()):
            continue
        try:
            with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                m = re.search(r'<CreationDate\s+value="([^"]+)"', fh.read())
            if m:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                off = dt.utcoffset()
                tz = None
                if off is not None:
                    mins = int(off.total_seconds() // 60)
                    tz = f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
                return dt.replace(tzinfo=None), tz
        except (OSError, ValueError):
            pass
    return None


def read_sources(exe, paths):
    """元動画の情報を読む。{正規化パス: {"path", "src": (datetime, tz, Keysを持つか) or None, "dur"}}

    撮影日時の優先順: Keys:CreationDate（iPhone等）> QuickTime:CreateDate > DateTimeOriginal（AVCHD等）
                    > Sony MXF の XML > MXF 内の CreateDate（タイムゾーンなし）
    src = (日時, タイムゾーン, Keysを持つか, 種類)  種類 "mxf" はタイムゾーン不明の MXF 日時
    """
    if not paths:
        return {}
    data = exiftool_json(exe, ["-G1", "-api", "QuickTimeUTC", "-" + KEYS_TAG,
                               "-QuickTime:CreateDate", "-DateTimeOriginal",
                               "-MXF:CreateDate", "-Duration#"]
                         + list(paths))
    result = {}
    for d in data:
        keys_val = d.get(KEYS_TAG)
        candidates = [keys_val, d.get("QuickTime:CreateDate")]
        candidates += [v for k, v in d.items() if k.endswith(":DateTimeOriginal")]
        path = d.get("SourceFile", "")
        src = None
        for c in candidates:
            parsed = parse_exif_dt(c)
            if parsed:
                src = (parsed[0], parsed[1], bool(parse_exif_dt(keys_val)), "normal")
                break
        if not src and path.lower().endswith(".mxf"):
            side = sony_sidecar_date(path)
            if side:
                src = (side[0], side[1], False, "normal")
            else:
                parsed = parse_exif_dt(d.get("MXF:CreateDate"))
                if parsed:
                    src = (parsed[0], None, False, "mxf")
        result[norm(path)] = {"path": path, "src": src, "dur": get_duration(d)}
    return result


def plan_matches(targets, sources, use_duration, tol):
    """書き出しファイルと元動画の対応を決める。

    targets: [(キー, パス, 長さ秒)]、sources: read_sources() の値のリスト
    1. ファイル名で対応付け
    2. 残りを長さ（±tol 秒）で 1対1 に対応付け（差が小さい組から順に確定）
    戻り値: {キー: (元動画パス, 方法)}  方法は "名前" / "長さ" / "長さ・要確認"
    """
    result = {}
    source_paths = [s["path"] for s in sources]
    for key, path, _ in targets:
        src = match_source(path, source_paths)
        if src:
            result[key] = (src, "名前")
    if not use_duration:
        return result

    used = {norm(p) for p, _ in result.values()}
    pool = [s for s in sources if s["dur"] and s["src"] and norm(s["path"]) not in used]
    rest = [t for t in targets if t[0] not in result and t[2]]
    cands = sorted((abs(td - s["dur"]), key, s["path"])
                   for key, _, td in rest for s in pool if abs(td - s["dur"]) <= tol)
    taken_t, taken_s = set(), set()
    for diff, key, sp in cands:
        if key in taken_t or norm(sp) in taken_s:
            continue
        taken_t.add(key)
        taken_s.add(norm(sp))
        # ほぼ同じ長さの元動画が他にもある場合は要確認にする
        rivals = [c for c in cands if c[1] == key and norm(c[2]) != norm(sp) and c[0] - diff < 0.1]
        result[key] = (sp, "長さ・要確認" if rivals else "長さ")
    return result


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.strptime(str(s)[:19], DISPLAY_FMT)
        return dt if dt.year > 1904 else None
    except ValueError:
        return None


def parse_offset(s):
    """'83' / '1:23' / '1:02:03' → 秒数。不正なら None。"""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        sec = 0.0
        for part in s.split(":"):
            sec = sec * 60 + float(part)
        return round(sec) if sec >= 0 else None
    except ValueError:
        return None


def date_from_filename(path):
    m = FILENAME_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def match_source(target_path, sources):
    """書き出しファイル名に元動画のファイル名（拡張子なし）が含まれるものを探す。

    IMG_1234.MOV → IMG_1234_edit.mp4 / IMG_1234 2.mp4 / 書き出し_IMG_1234.mp4 など。
    IMG_123 が IMG_1234 に誤マッチしないよう、直後が英数字でないことを条件にする。
    """
    t_stem = os.path.splitext(os.path.basename(target_path))[0]
    best = None
    for src in sources:
        if norm(src) == norm(target_path):
            continue
        s_stem = os.path.splitext(os.path.basename(src))[0]
        if len(s_stem) < 3:
            continue
        pat = r"(?<![0-9A-Za-z])" + re.escape(s_stem) + r"(?![0-9A-Za-z])"
        if re.search(pat, t_stem, re.IGNORECASE):
            if best is None or len(s_stem) > len(os.path.splitext(os.path.basename(best))[0]):
                best = src
    return best


def fmt_dur(sec):
    if sec is None:
        return ""
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}"


def build_write_args(item, mode, new_dt, tz, shift, backup, write_keys):
    tags = DATE_TAGS + ([KEYS_TAG] if write_keys else [])
    args = ["-api", "QuickTimeUTC", "-m"]
    if mode == "shift":
        op, shift_str = shift
        args += [f"-{t}{op}={shift_str}" for t in tags]
    else:
        value = new_dt.strftime("%Y:%m:%d %H:%M:%S") + tz
        args += [f"-{t}={value}" for t in tags]
    if not backup:
        args.append("-overwrite_original")
    args.append(item["path"])
    return args


# ---------------------------------------------------------------- GUI

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("動画の撮影日時エディタ")
        self.geometry("1020x680")
        self.minsize(820, 560)
        self.exe = find_exiftool()
        self.items = []
        self.busy = False

        self.mode = tk.StringVar(value="set")
        self.set_value = tk.StringVar(value=datetime.now().strftime(DISPLAY_FMT))
        self.tz = tk.StringVar(value="+09:00")
        self.sign = tk.StringVar(value="+")
        self.sh = {k: tk.StringVar(value="0") for k in ("d", "h", "m", "s")}
        self.offset = tk.StringVar(value="0")
        self.use_dur = tk.BooleanVar(value=True)
        self.dur_tol = tk.StringVar(value="0.5")
        self.mxf_utc = tk.BooleanVar(value=True)
        self.backup = tk.BooleanVar(value=True)
        self.sync_filedate = tk.BooleanVar(value=True)
        self.status = tk.StringVar()

        theme = os.environ.get("VDE_THEME")  # 例: VDE_THEME=clam で描画方式を変更
        if theme:
            ttk.Style(self).theme_use(theme)

        self._build()
        self.after(150, self._bring_to_front)
        for v in [self.mode, self.set_value, self.tz, self.sign, self.offset, self.mxf_utc,
                  *self.sh.values()]:
            v.trace_add("write", lambda *_: self.refresh_preview())
        self._update_mode_widgets()
        self._set_initial_status()

    # ---- layout
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        hint = {"foreground": "#888"}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="ファイルを追加", command=self.add_files).pack(side="left")
        ttk.Button(top, text="フォルダを追加", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="選択行を外す", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="すべて外す", command=self.clear).pack(side="left", padx=4)
        ttk.Button(top, text="ExifTool の場所…", command=self.pick_exiftool).pack(side="right")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("file", "dur", "current", "source", "new")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, label, w in [("file", "ファイル", 260), ("dur", "長さ", 70),
                            ("current", "現在の撮影日時", 140),
                            ("source", "元動画", 250), ("new", "変更後", 190)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("skip", foreground="#999999")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        box = ttk.LabelFrame(self, text="変更方法")
        box.pack(fill="x", **pad)

        # 元動画からコピー
        rc = ttk.Frame(box)
        rc.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(rc, text="元動画に合わせる", variable=self.mode, value="copy",
                        command=self._update_mode_widgets, width=14).pack(side="left")
        ttk.Button(rc, text="元動画ファイルを選んで自動対応付け…",
                   command=lambda: self.auto_match(folder=False)).pack(side="left")
        ttk.Button(rc, text="元動画フォルダから自動対応付け…",
                   command=lambda: self.auto_match(folder=True)).pack(side="left", padx=4)
        ttk.Button(rc, text="選択行の元動画を手動で選ぶ…", command=self.pick_source).pack(side="left")
        rcd = ttk.Frame(box)
        rcd.pack(fill="x", padx=6, pady=(0, 3))
        ttk.Label(rcd, text="", width=14).pack(side="left")
        ttk.Checkbutton(rcd, text="ファイル名が違っても長さが同じなら対応付ける　許容差 ±",
                        variable=self.use_dur).pack(side="left")
        ttk.Entry(rcd, textvariable=self.dur_tol, width=5).pack(side="left")
        ttk.Label(rcd, text="秒").pack(side="left")
        rcm = ttk.Frame(box)
        rcm.pack(fill="x", padx=6, pady=(0, 3))
        ttk.Label(rcm, text="", width=14).pack(side="left")
        ttk.Checkbutton(rcm, text="MXF の日時を UTC として扱う", variable=self.mxf_utc).pack(side="left")
        ttk.Label(rcm, text="（XML が無い MXF のみ。変更後が 9 時間ずれていたら切り替え）",
                  foreground="#888").pack(side="left")
        rc2 = ttk.Frame(box)
        rc2.pack(fill="x", padx=6, pady=(0, 3))
        ttk.Label(rc2, text="", width=14).pack(side="left")
        ttk.Label(rc2, text="書き出し開始位置").pack(side="left")
        self.offset_entry = ttk.Entry(rc2, textvariable=self.offset, width=8)
        self.offset_entry.pack(side="left", padx=4)
        ttk.Label(rc2, text="元動画の途中から書き出した場合に入力（例: 1:23 = 1分23秒）", **hint).pack(side="left")

        ttk.Separator(box).pack(fill="x", padx=6, pady=3)

        r1 = ttk.Frame(box)
        r1.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(r1, text="日時を指定", variable=self.mode, value="set",
                        command=self._update_mode_widgets, width=14).pack(side="left")
        self.set_entry = ttk.Entry(r1, textvariable=self.set_value, width=22)
        self.set_entry.pack(side="left")
        self.copy_btn = ttk.Button(r1, text="選択行の日時を入れる", command=self.copy_from_selected)
        self.copy_btn.pack(side="left", padx=6)
        ttk.Label(r1, text="例: 2024-05-01 12:00:00", **hint).pack(side="left")

        r2 = ttk.Frame(box)
        r2.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(r2, text="時間をずらす", variable=self.mode, value="shift",
                        command=self._update_mode_widgets, width=14).pack(side="left")
        self.sign_box = ttk.Combobox(r2, textvariable=self.sign, values=["+", "-"],
                                     width=3, state="readonly")
        self.sign_box.pack(side="left")
        self.shift_spins = []
        for key, unit, mx in [("d", "日", 9999), ("h", "時間", 23), ("m", "分", 59), ("s", "秒", 59)]:
            sp = ttk.Spinbox(r2, from_=0, to=mx, textvariable=self.sh[key], width=5)
            sp.pack(side="left", padx=(6, 0))
            ttk.Label(r2, text=unit).pack(side="left")
            self.shift_spins.append(sp)

        r3 = ttk.Frame(box)
        r3.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(r3, text="ファイル名から", variable=self.mode, value="filename",
                        command=self._update_mode_widgets, width=14).pack(side="left")
        ttk.Label(r3, text="VID_20240501_120000.mp4 や PXL_20240501_120000123.mp4 などから読み取ります",
                  **hint).pack(side="left")

        r4 = ttk.Frame(box)
        r4.pack(fill="x", padx=6, pady=(3, 6))
        ttk.Label(r4, text="タイムゾーン").pack(side="left")
        self.tz_entry = ttk.Entry(r4, textvariable=self.tz, width=8)
        self.tz_entry.pack(side="left", padx=4)
        ttk.Label(r4, text="（日本は +09:00。元動画に合わせる場合は、元動画にタイムゾーン情報がないときだけ使用）",
                  **hint).pack(side="left")

        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        ttk.Checkbutton(opt, text="元ファイルを残す（*_original）", variable=self.backup).pack(side="left")
        ttk.Checkbutton(opt, text="ファイルの更新日時も撮影日時に合わせる",
                        variable=self.sync_filedate).pack(side="left", padx=12)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.apply_btn = ttk.Button(bottom, text="変更を書き込む", command=self.apply)
        self.apply_btn.pack(side="right")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=180)
        self.progress.pack(side="right", padx=8)
        ttk.Label(bottom, textvariable=self.status).pack(side="left")

    def _bring_to_front(self):
        """Mac でウィンドウがターミナルの裏に隠れる・描画されない問題への対策。"""
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        w, h = self.winfo_width(), self.winfo_height()
        if w > 1 and h > 1:
            self.geometry(f"{w + 1}x{h}")
            self.after(50, lambda: self.geometry(f"{w}x{h}"))

    def _set_initial_status(self):
        if self.exe:
            self.status.set("動画ファイルを追加してください")
        else:
            self.status.set("ExifTool が見つかりません。右上の「ExifTool の場所…」で指定してください")

    def _update_mode_widgets(self):
        m = self.mode.get()
        st = lambda on: "normal" if on else "disabled"
        self.set_entry.configure(state=st(m == "set"))
        self.copy_btn.configure(state=st(m == "set"))
        self.sign_box.configure(state="readonly" if m == "shift" else "disabled")
        for sp in self.shift_spins:
            sp.configure(state=st(m == "shift"))
        self.offset_entry.configure(state=st(m == "copy"))
        self.tz_entry.configure(state=st(m != "shift"))
        self.refresh_preview()

    # ---- file list
    def pick_exiftool(self):
        p = filedialog.askopenfilename(title="exiftool を選択")
        if p:
            self.exe = p
            self.status.set("ExifTool を設定しました")
            self.reload_dates()

    def _require_exiftool(self):
        if not self.exe:
            messagebox.showerror(
                "ExifTool が必要です",
                "https://exiftool.org/ からインストールするか、「ExifTool の場所…」で指定してください。")
            return False
        return True

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="書き換える動画を選択",
            filetypes=[("動画", tuple("*" + e for e in sorted(VIDEO_EXTS))), ("すべて", "*")])
        self._add_paths(paths)

    def add_folder(self):
        d = filedialog.askdirectory(title="フォルダを選択")
        if d:
            self._add_paths(self._videos_in(d, VIDEO_EXTS))

    @staticmethod
    def _videos_in(folder, exts):
        found = []
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in exts and not f.startswith("._"):
                    found.append(os.path.join(root, f))
        return sorted(found)

    def _add_paths(self, paths):
        if not paths or not self._require_exiftool():
            return
        known = {norm(i["path"]) for i in self.items}
        for p in paths:
            if norm(p) not in known:
                self.items.append({"path": os.path.normpath(p), "current": None, "current_str": "",
                                   "has_keys": False, "source": None, "src": None,
                                   "dur": None, "match": ""})
                known.add(norm(p))
        self.reload_dates()

    def remove_selected(self):
        sel = {int(i) for i in self.tree.selection()}
        self.items = [it for n, it in enumerate(self.items) if n not in sel]
        self.refresh_preview()

    def clear(self):
        self.items = []
        self.refresh_preview()

    def reload_dates(self):
        if not self.items or not self.exe:
            self.refresh_preview()
            return
        self.status.set("撮影日時を読み込み中…")
        self.update_idletasks()
        info = read_dates(self.exe, [i["path"] for i in self.items])
        for it in self.items:
            cd, kd, dur = info.get(norm(it["path"]), (None, None, None))
            it["dur"] = dur
            it["current"] = parse_dt(cd)
            it["current_str"] = it["current"].strftime(DISPLAY_FMT) if it["current"] else "（なし）"
            it["has_keys"] = bool(kd)
        self.status.set(f"{len(self.items)} 件")
        self.refresh_preview()

    def copy_from_selected(self):
        for iid in self.tree.selection():
            cur = self.items[int(iid)]["current"]
            if cur:
                self.set_value.set(cur.strftime(DISPLAY_FMT))
                return

    # ---- 元動画
    def _assign_sources(self, pairs, info=None):
        """pairs: [(item, source_path, 方法)] を設定する。"""
        if info is None:
            info = read_sources(self.exe, sorted({s for _, s, _ in pairs}))
        missing = []
        for it, src, how in pairs:
            entry = info.get(norm(src))
            it["source"] = src
            it["src"] = entry["src"] if entry else None
            it["match"] = how
            if not it["src"]:
                missing.append(os.path.basename(src))
        self.mode.set("copy")
        self._update_mode_widgets()
        if missing:
            messagebox.showwarning("撮影日時がない元動画",
                                   "次の元動画からは撮影日時を読み取れませんでした:\n" + "\n".join(missing[:20]))

    def pick_source(self):
        if not self._require_exiftool():
            return
        sel = [self.items[int(i)] for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo("行を選択してください",
                                "一覧で書き出したファイルを選んでから、元動画を指定してください。\n"
                                "複数行を選ぶと、同じ元動画をまとめて設定できます。")
            return
        p = filedialog.askopenfilename(
            title="元動画（撮影したままのファイル）を選択",
            filetypes=[("動画", tuple("*" + e for e in sorted(SOURCE_EXTS))), ("すべて", "*")])
        if p:
            self._assign_sources([(it, p, "手動") for it in sel])

    def auto_match(self, folder):
        if not self._require_exiftool():
            return
        if not self.items:
            messagebox.showinfo("ファイルがありません", "先に書き出したファイルを追加してください。")
            return
        try:
            tol = float(self.dur_tol.get())
            assert tol >= 0
        except (ValueError, AssertionError):
            messagebox.showerror("許容差", "許容差は 0 以上の秒数で入力してください（例: 0.5）。")
            return
        if folder:
            d = filedialog.askdirectory(title="元動画が入っているフォルダを選択")
            paths = self._videos_in(d, SOURCE_EXTS) if d else []
        else:
            paths = list(filedialog.askopenfilenames(
                title="元動画を選択（複数まとめて選べます）",
                filetypes=[("動画", tuple("*" + e for e in sorted(SOURCE_EXTS))), ("すべて", "*")]))
        own = {norm(it["path"]) for it in self.items}
        paths = [p for p in paths if norm(p) not in own]
        if not paths:
            return

        self.status.set(f"元動画 {len(paths)} 件を読み込み中…")
        self.update_idletasks()
        info = read_sources(self.exe, paths)
        targets = [(n, it["path"], it["dur"]) for n, it in enumerate(self.items)]
        plan = plan_matches(targets, list(info.values()), self.use_dur.get(), tol)
        pairs = [(self.items[n], src, how) for n, (src, how) in plan.items()]
        if pairs:
            self._assign_sources(pairs, info)

        unmatched = [os.path.basename(it["path"]) for n, it in enumerate(self.items) if n not in plan]
        check = [os.path.basename(self.items[n]["path"]) for n, (_, how) in plan.items() if "要確認" in how]
        by_name = sum(1 for _, how in plan.values() if how == "名前")
        msg = f"{len(pairs)} 件を対応付けました（名前 {by_name} 件 / 長さ {len(pairs) - by_name} 件）。"
        if check:
            msg += ("\n\n長さがほぼ同じ元動画が複数あったもの（「元動画」列と変更後の日時を確認してください）:\n"
                    + "\n".join(check[:20]))
        if unmatched:
            msg += ("\n\n見つからなかったもの（「選択行の元動画を手動で選ぶ…」で指定できます）:\n"
                    + "\n".join(unmatched[:20]))
        self.status.set(f"{len(self.items)} 件")
        messagebox.showinfo("自動対応付け", msg)

    # ---- preview
    def _shift_delta(self):
        try:
            vals = {k: int(v.get() or 0) for k, v in self.sh.items()}
        except ValueError:
            return None
        if any(v < 0 for v in vals.values()):
            return None
        return timedelta(days=vals["d"], hours=vals["h"], minutes=vals["m"], seconds=vals["s"])

    def compute_new(self, item):
        """(新しい日時, タイムゾーン) を返す。変更できないときは None。"""
        m = self.mode.get()
        tz = self.tz.get().strip()
        if m == "set":
            dt = parse_dt(self.set_value.get().strip().replace("/", "-"))
            return (dt, tz) if dt else None
        if m == "shift":
            delta = self._shift_delta()
            if item["current"] is None or not delta:
                return None
            dt = item["current"] + delta if self.sign.get() == "+" else item["current"] - delta
            return dt, None
        if m == "copy":
            off = parse_offset(self.offset.get())
            if not item["src"] or off is None:
                return None
            src_dt, src_tz, _, kind = item["src"]
            if kind == "mxf":  # タイムゾーン情報なし
                if self.mxf_utc.get() and TZ_RE.match(tz):
                    src_dt += tz_delta(tz)
                src_tz = None
            return src_dt + timedelta(seconds=off), (src_tz or tz)
        dt = date_from_filename(item["path"])
        return (dt, tz) if dt else None

    def refresh_preview(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for n, it in enumerate(self.items):
            new = self.compute_new(it)
            if new:
                new_str = new[0].strftime(DISPLAY_FMT) + (f" ({new[1]})" if new[1] else "")
            else:
                new_str = "—"
            src_str = os.path.basename(it["source"]) if it["source"] else ""
            if it["source"]:
                src_str += "（日時なし）" if not it["src"] else f"（{it['match']}）"
            self.tree.insert("", "end", iid=str(n), values=(
                os.path.basename(it["path"]), fmt_dur(it["dur"]), it["current_str"],
                src_str, new_str),
                tags=(() if new else ("skip",)))
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

    # ---- write
    def apply(self):
        if self.busy or not self._require_exiftool():
            return
        if not self.items:
            messagebox.showinfo("ファイルがありません", "先に動画ファイルを追加してください。")
            return
        mode = self.mode.get()
        if mode != "shift" and not TZ_RE.match(self.tz.get().strip()):
            messagebox.showerror("タイムゾーンの形式", "タイムゾーンは +09:00 の形式で入力してください。")
            return
        if mode == "set" and not parse_dt(self.set_value.get().strip().replace("/", "-")):
            messagebox.showerror("日時の形式", "日時は 2024-05-01 12:00:00 の形式で入力してください。")
            return
        if mode == "copy" and parse_offset(self.offset.get()) is None:
            messagebox.showerror("書き出し開始位置", "秒数か 分:秒 の形式で入力してください（例: 83 または 1:23）。")
            return
        shift = None
        if mode == "shift":
            if not self._shift_delta():
                messagebox.showerror("ずらす時間", "0 以上の整数で、0 以外の時間を入力してください。")
                return
            v = {k: int(var.get() or 0) for k, var in self.sh.items()}
            shift = (self.sign.get(), f"0:0:{v['d']} {v['h']}:{v['m']}:{v['s']}")

        targets = [(it, self.compute_new(it)) for it in self.items]
        targets = [(it, nd) for it, nd in targets if nd]
        if not targets:
            hint = "（元動画を対応付けてください）" if mode == "copy" else ""
            messagebox.showinfo("変更なし", "変更できるファイルがありません" + hint)
            return
        msg = f"{len(targets)} 件のファイルの撮影日時を書き換えます。"
        if not self.backup.get():
            msg += "\n元ファイルは残りません。"
        if not messagebox.askokcancel("確認", msg):
            return

        self.busy = True
        self.apply_btn.configure(state="disabled")
        self.progress.configure(maximum=len(targets), value=0)
        opts = (mode, shift, self.backup.get(), self.sync_filedate.get())
        threading.Thread(target=self._worker, args=(targets, opts), daemon=True).start()

    def _worker(self, targets, opts):
        mode, shift, backup, sync = opts
        errors = []
        for n, (it, (new_dt, tz)) in enumerate(targets, 1):
            name = os.path.basename(it["path"])
            self.after(0, self.status.set, f"書き込み中 ({n}/{len(targets)}): {name}")
            # 元動画が iPhone 形式の日時を持っていれば、書き出しファイルにも付ける
            write_keys = it["has_keys"] or (mode == "copy" and it["src"] and it["src"][2])
            args = build_write_args(it, mode, new_dt, tz, shift, backup, write_keys)
            rc, out, err = run_exiftool(self.exe, args)
            if rc != 0 or "1 image files updated" not in out:
                lines = (err or out).strip().splitlines()
                errors.append(f"{name}: {lines[-1] if lines else '不明なエラー'}")
            elif sync:
                run_exiftool(self.exe, ["-api", "QuickTimeUTC",
                                        "-FileModifyDate<QuickTime:CreateDate", it["path"]])
            self.after(0, self.progress.configure, {"value": n})
        self.after(0, self._done, len(targets), errors)

    def _done(self, total, errors):
        self.busy = False
        self.apply_btn.configure(state="normal")
        self.reload_dates()
        ok = total - len(errors)
        self.status.set(f"完了: {ok} 件成功" + (f" / {len(errors)} 件失敗" if errors else ""))
        if errors:
            messagebox.showwarning("一部失敗しました", "\n".join(errors[:20]))


if __name__ == "__main__":
    App().mainloop()
