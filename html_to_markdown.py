# -*- coding: utf-8 -*-
import os, re, sys, shutil, base64, mimetypes, threading, traceback
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from bs4 import BeautifulSoup, NavigableString, Tag, Comment

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except Exception:
    BaseTk = tk.Tk
    HAS_DND = False

APP_NAME = "HTML → Markdown"
PAGE_RE = re.compile(r'^\s*ص\s*:\s*([0-9٠-٩۰-۹]+)\s*$')
ONLY_STARS_RE = re.compile(r'^\s*\*\*\s*$')
REMOTE_RE = re.compile(r'^(?:https?:)?//', re.I)

def read_html(path):
    data = Path(path).read_bytes()
    # BeautifulSoup/lxml generally handles declared encodings well.
    soup = BeautifulSoup(data, "lxml")
    return soup

def esc_text(s):
    # Preserve source text; only protect Markdown syntax that could accidentally
    # change plain text semantics. Do not normalize Arabic/Persian Unicode.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s

def clean_inline_ws(s):
    # Only collapse HTML layout whitespace, not meaningful paragraph text.
    s = re.sub(r'[\t\f\v]+', ' ', s)
    s = re.sub(r' {2,}', ' ', s)
    return s.strip()

class Converter:
    def __init__(self, source, out_root, preserve_tree=False, root_input=None):
        self.source = Path(source)
        self.out_root = Path(out_root)
        self.preserve_tree = preserve_tree
        self.root_input = Path(root_input) if root_input else self.source.parent
        self.warnings = []
        self.assets_dir = None
        self.md_path = None
        self._image_names = set()

    def warn(self, msg):
        self.warnings.append(msg)

    def output_paths(self):
        stem = self.source.stem
        if self.preserve_tree:
            try:
                rel_dir = self.source.parent.relative_to(self.root_input)
            except Exception:
                rel_dir = Path()
        else:
            rel_dir = Path()
        target_dir = self.out_root / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        self.md_path = target_dir / f"{stem}.md"
        self.assets_dir = target_dir / f"{stem}_assets"
        return self.md_path

    def unique_image_name(self, name):
        name = Path(name).name or "image"
        stem, suf = os.path.splitext(name)
        if not suf:
            suf = ".bin"
        candidate = stem + suf
        n = 2
        while candidate.lower() in self._image_names or (self.assets_dir / candidate).exists():
            candidate = f"{stem}_{n}{suf}"
            n += 1
        self._image_names.add(candidate.lower())
        return candidate

    def handle_image(self, tag):
        src = (tag.get("src") or "").strip()
        alt = tag.get("alt") or ""
        title = tag.get("title")
        if not src:
            self.warn("صورة بلا مسار src.")
            return "<!-- صورة بلا مسار في المصدر -->"
        if REMOTE_RE.match(src):
            suffix = f' "{title}"' if title else ""
            return f"![{alt}]({src}{suffix})"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        if src.lower().startswith("data:image/"):
            try:
                head, payload = src.split(",", 1)
                mime = head.split(";")[0].split(":", 1)[1]
                ext = mimetypes.guess_extension(mime) or ".img"
                name = self.unique_image_name("embedded" + ext)
                raw = base64.b64decode(payload)
                (self.assets_dir / name).write_bytes(raw)
                rel = f"{self.assets_dir.name}/{name}"
                suffix = f' "{title}"' if title else ""
                return f"![{alt}]({rel}{suffix})"
            except Exception as e:
                self.warn(f"تعذر استخراج صورة مضمّنة: {e}")
                return "<!-- تعذر استخراج صورة مضمّنة من المصدر -->"
        local = (self.source.parent / src).resolve()
        if local.exists() and local.is_file():
            name = self.unique_image_name(local.name)
            shutil.copy2(local, self.assets_dir / name)
            rel = f"{self.assets_dir.name}/{name}"
            suffix = f' "{title}"' if title else ""
            return f"![{alt}]({rel}{suffix})"
        self.warn(f"صورة محلية مفقودة: {src}")
        return f"<!-- صورة مفقودة من المصدر: {src} -->"

    def inline(self, node):
        if isinstance(node, NavigableString):
            if isinstance(node, Comment):
                txt = str(node).strip()
                return f"<!-- {txt} -->" if txt else ""
            return esc_text(str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        content = "".join(self.inline(c) for c in node.children)
        if name in ("script", "style", "noscript"):
            return ""
        if name == "br":
            return "  \n"
        if name in ("strong", "b"):
            return f"**{content}**"
        if name in ("em", "i"):
            return f"*{content}*"
        if name == "u":
            return f"<u>{content}</u>"
        if name == "sup":
            return f"<sup>{content}</sup>"
        if name == "sub":
            return f"<sub>{content}</sub>"
        if name == "code":
            escaped = content.replace('`', '\\`')
            return f"`{escaped}`"
        if name == "a":
            href = (node.get("href") or "").strip()
            text = content.strip() or href
            return f"[{text}]({href})" if href else text
        if name == "img":
            return self.handle_image(node)
        return content

    def simple_table(self, table):
        rows = table.find_all("tr", recursive=True)
        matrix = []
        complex_table = False
        for tr in rows:
            cells = tr.find_all(["th", "td"], recursive=False)
            row = []
            for c in cells:
                if c.get("rowspan") not in (None, "1") or c.get("colspan") not in (None, "1"):
                    complex_table = True
                row.append(clean_inline_ws(self.inline(c)).replace("|", r"\|"))
            if row:
                matrix.append(row)
        if complex_table or not matrix:
            self.warn("جدول معقد تم الاحتفاظ به كـ HTML.")
            return str(table)
        width = max(map(len, matrix))
        matrix = [r + [""] * (width-len(r)) for r in matrix]
        header = matrix[0]
        body = matrix[1:]
        out = ["| " + " | ".join(header) + " |",
               "| " + " | ".join(["---"]*width) + " |"]
        out += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(out)

    def block(self, node):
        if isinstance(node, NavigableString):
            t = clean_inline_ws(str(node))
            return t if t else ""
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name in ("script", "style", "noscript", "head"):
            return ""
        if name in [f"h{i}" for i in range(1,7)]:
            lvl = int(name[1])
            text = clean_inline_ws(self.inline(node))
            return f"{'#'*lvl} {text}" if text else ""
        # Handle nonstandard class-based heading levels conservatively.
        classes = node.get("class") or []
        for cls in classes:
            m = re.fullmatch(r"content_h(\d+)", cls)
            if m and name not in [f"h{i}" for i in range(1,7)]:
                lvl = min(int(m.group(1)), 6)
                text = clean_inline_ws(self.inline(node))
                return f"{'#'*lvl} {text}" if text else ""
        if name == "p":
            text = clean_inline_ws(self.inline(node))
            if not text:
                return ""
            m = PAGE_RE.match(text)
            if m:
                return f"<!-- صفحة {m.group(1)} -->"
            if ONLY_STARS_RE.match(text):
                return "---"
            return text
        if name == "blockquote":
            text = self.inline(node).strip()
            return "\n".join("> " + line for line in text.splitlines())
        if name == "pre":
            return "```\n" + node.get_text("", strip=False).rstrip() + "\n```"
        if name == "table":
            return self.simple_table(node)
        if name in ("ul", "ol"):
            lines = []
            ordered = name == "ol"
            for idx, li in enumerate(node.find_all("li", recursive=False), 1):
                prefix = f"{idx}. " if ordered else "- "
                txt = clean_inline_ws("".join(self.inline(c) for c in li.contents
                                               if not (isinstance(c, Tag) and c.name in ("ul","ol"))))
                lines.append(prefix + txt)
                for sub in li.find_all(["ul","ol"], recursive=False):
                    nested = self.block(sub)
                    lines += ["  " + x for x in nested.splitlines()]
            return "\n".join(lines)
        if name == "img":
            return self.handle_image(node)
        if name == "hr":
            return "---"
        if name == "span" and "chapter" in (node.get("class") or []):
            return ""  # page boundary marker itself is not semantic content
        if name in ("div", "section", "article", "main", "body"):
            parts = []
            for c in node.children:
                b = self.block(c)
                if b:
                    parts.append(b)
            return "\n\n".join(parts)
        # Unknown element: preserve its text at minimum.
        txt = clean_inline_ws(self.inline(node))
        if txt:
            if name not in ("span", "font", "center", "small", "big"):
                self.warn(f"عنصر HTML غير معتاد <{name}>: تم الحفاظ على محتواه النصي.")
            return txt
        return ""

    def convert(self):
        self.output_paths()
        soup = read_html(self.source)
        body = soup.body or soup
        md = self.block(body)
        md = re.sub(r'\n{3,}', '\n\n', md).strip() + "\n"
        self.md_path.write_text(md, encoding="utf-8", newline="\n")
        # Remove empty assets folder.
        if self.assets_dir.exists() and not any(self.assets_dir.iterdir()):
            self.assets_dir.rmdir()
        return self.md_path, self.warnings

class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("920x680")
        self.minsize(780, 560)
        self.files = []
        self.root_input = None
        self.cancelled = False
        self.out_dir = tk.StringVar(value=str(Path.home() / "Documents" / "Markdown"))
        self.include_sub = tk.BooleanVar(value=True)
        self.preserve_tree = tk.BooleanVar(value=True)
        self.make_report = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="جاهز للتحويل")
        self.build_ui()

    def build_ui(self):
        self.option_add("*Font", ("Segoe UI", 10))
        main = ttk.Frame(self, padding=18)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="HTML → Markdown", font=("Segoe UI", 20, "bold")).pack(anchor="center", pady=(0,12))
        drop = ttk.LabelFrame(main, text="ملفات HTML", padding=14)
        drop.pack(fill="x")
        self.drop_label = ttk.Label(drop, text="اسحب ملفات HTML أو مجلدًا إلى هنا\nأو استخدم أزرار الاختيار", anchor="center", justify="center")
        self.drop_label.pack(fill="x", pady=12)
        btns = ttk.Frame(drop); btns.pack()
        ttk.Button(btns, text="اختيار ملفات", command=self.pick_files).pack(side="left", padx=5)
        ttk.Button(btns, text="اختيار مجلد", command=self.pick_folder).pack(side="left", padx=5)
        ttk.Checkbutton(drop, text="تضمين المجلدات الفرعية", variable=self.include_sub).pack(anchor="center", pady=(8,0))
        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

        listf = ttk.LabelFrame(main, text="الملفات المحددة", padding=8)
        listf.pack(fill="both", expand=True, pady=12)
        self.tree = ttk.Treeview(listf, columns=("status",), show="tree headings", height=10)
        self.tree.heading("#0", text="الملف")
        self.tree.heading("status", text="الحالة")
        self.tree.column("status", width=150, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(listf, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y"); self.tree.configure(yscrollcommand=sb.set)

        out = ttk.LabelFrame(main, text="مكان الحفظ", padding=10)
        out.pack(fill="x")
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="اختيار", command=self.pick_output).pack(side="left", padx=(8,0))

        opts = ttk.Frame(main); opts.pack(fill="x", pady=8)
        ttk.Checkbutton(opts, text="الحفاظ على بنية المجلدات", variable=self.preserve_tree).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="إنشاء تقرير عند وجود مشكلات", variable=self.make_report).pack(side="left", padx=6)

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(4,6))
        bottom = ttk.Frame(main); bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        self.start_btn = ttk.Button(bottom, text="بدء التحويل", command=self.start)
        self.start_btn.pack(side="right")
        ttk.Button(bottom, text="مسح القائمة", command=self.clear).pack(side="right", padx=8)

    def add_files(self, paths, root=None):
        exts = {".html",".htm"}
        found = []
        for p in map(Path, paths):
            if p.is_dir():
                pat = "**/*" if self.include_sub.get() else "*"
                found += [x for x in p.glob(pat) if x.is_file() and x.suffix.lower() in exts]
                root = root or p
            elif p.is_file() and p.suffix.lower() in exts:
                found.append(p)
        known = {str(x.resolve()) for x in self.files}
        for p in found:
            rp = str(p.resolve())
            if rp not in known:
                self.files.append(p)
                self.tree.insert("", "end", iid=rp, text=p.name, values=("في الانتظار",))
                known.add(rp)
        if root:
            self.root_input = Path(root)
        self.status.set(f"تم تحديد {len(self.files)} ملفًا")

    def pick_files(self):
        fs = filedialog.askopenfilenames(filetypes=[("HTML files","*.html *.htm"),("All files","*.*")])
        if fs: self.add_files(fs)

    def pick_folder(self):
        d = filedialog.askdirectory()
        if d: self.add_files([d], root=d)

    def pick_output(self):
        d = filedialog.askdirectory()
        if d: self.out_dir.set(d)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        self.add_files(paths, root=paths[0] if len(paths)==1 and Path(paths[0]).is_dir() else None)

    def clear(self):
        self.files.clear(); self.root_input=None
        for x in self.tree.get_children(): self.tree.delete(x)
        self.status.set("جاهز للتحويل")

    def start(self):
        if not self.files:
            messagebox.showwarning(APP_NAME, "اختر ملف HTML واحدًا على الأقل.")
            return
        out = Path(self.out_dir.get())
        out.mkdir(parents=True, exist_ok=True)
        self.start_btn.config(state="disabled")
        self.progress["maximum"] = len(self.files)
        self.progress["value"] = 0
        threading.Thread(target=self.run_conversion, daemon=True).start()

    def run_conversion(self):
        all_warnings = []
        ok = review = failed = 0
        for i,p in enumerate(self.files,1):
            iid = str(p.resolve())
            self.after(0, lambda iid=iid: self.tree.set(iid, "status", "جاري التحويل"))
            self.after(0, lambda p=p,i=i: self.status.set(f"جاري التحويل: {p.name} — {i} من {len(self.files)}"))
            try:
                conv = Converter(p, self.out_dir.get(), self.preserve_tree.get(),
                                 self.root_input or p.parent)
                md, warnings = conv.convert()
                if warnings:
                    review += 1
                    all_warnings.append((p, warnings))
                    st = "يحتاج مراجعة"
                else:
                    ok += 1
                    st = "تم التحويل"
                self.after(0, lambda iid=iid,st=st: self.tree.set(iid, "status", st))
            except Exception as e:
                failed += 1
                all_warnings.append((p, [f"فشل التحويل: {e}", traceback.format_exc()]))
                self.after(0, lambda iid=iid: self.tree.set(iid, "status", "فشل"))
            self.after(0, lambda i=i: self.progress.configure(value=i))
        report_path = None
        if all_warnings and self.make_report.get():
            report_path = Path(self.out_dir.get()) / "conversion_report.txt"
            lines = ["تقرير تحويل HTML إلى Markdown", "="*40, ""]
            for p, warns in all_warnings:
                lines.append(str(p))
                lines += [f"  - {w}" for w in warns]
                lines.append("")
            report_path.write_text("\n".join(lines), encoding="utf-8")
        total_success = ok + review
        msg = f"اكتمل التحويل.\n\nتم التحويل دون ملاحظات: {ok}\nيحتاج مراجعة: {review}\nفشل: {failed}"
        if report_path: msg += f"\n\nالتقرير:\n{report_path}"
        self.after(0, lambda: self.finish(msg))

    def finish(self, msg):
        self.start_btn.config(state="normal")
        self.status.set("اكتمل التحويل")
        messagebox.showinfo(APP_NAME, msg)

if __name__ == "__main__":
    App().mainloop()
