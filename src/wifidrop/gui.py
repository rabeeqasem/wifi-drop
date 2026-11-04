"""GTK4 / libadwaita desktop app: drag files onto the window, scan the QR code with the iPhone."""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .server import DropServer, human  # noqa: E402

try:
    import qrcode
except ImportError:  # the app still works, just without the QR image
    qrcode = None

CSS = """
.dropzone {
  border: 2px dashed alpha(currentColor, 0.25);
  border-radius: 18px;
  transition: all 150ms ease-out;
}
.drop-hover .dropzone {
  border-color: @accent_color;
  background-color: alpha(@accent_color, 0.10);
}
.qr-frame {
  background: white;
  border-radius: 14px;
  padding: 10px;
}
.side-card {
  background-color: alpha(currentColor, 0.05);
  border-radius: 18px;
  padding: 20px;
}
.drop-hint { padding: 14px; }
"""


class QRCodeView(Gtk.Picture):
    """Shows a QR code rendered into a texture (no cairo bindings needed)."""

    SCALE = 10  # pixels per QR module; large enough to stay crisp when scaled down

    def __init__(self, size=220):
        super().__init__(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.set_size_request(size, size)

    def set_text(self, text):
        if qrcode is None:
            self.set_paintable(None)
            return
        qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        s = self.SCALE
        rows = []
        for row in qr.get_matrix():
            line = b"".join(b"\x00" * s if on else b"\xff" * s for on in row)
            rows.extend([line] * s)
        size = len(rows)
        data = GLib.Bytes.new(b"".join(rows))
        texture = Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.G8, data, size) \
            if hasattr(Gdk.MemoryFormat, "G8") else self._rgb_texture(rows, size)
        self.set_paintable(texture)

    @staticmethod
    def _rgb_texture(rows, size):
        rgb = b"".join(bytes(b for px in line for b in (px, px, px)) for line in rows)
        return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(rgb), size * 3)


class DropWindow(Adw.ApplicationWindow):
    def __init__(self, app, server):
        super().__init__(application=app, title="Wi-Fi Drop")
        self.server = server
        self.set_default_size(860, 560)
        self._url = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Wi-Fi Drop", subtitle="Send files to your iPhone"))
        add_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add Files"))
        add_btn.connect("clicked", lambda *_: self.choose_files())
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("Open Received Folder", "app.open-received")
        menu.append("About Wi-Fi Drop", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        toolbar.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        self.root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24,
                            margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)
        self.toasts.set_child(self.root)

        # ---- left: drop zone / file list ----
        self.stack = Gtk.Stack(hexpand=True, vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root.append(self.stack)

        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.FILL)
        empty.add_css_class("dropzone")
        status = Adw.StatusPage(icon_name="document-send-symbolic", title="Drop Files Here",
                                description="PDFs, photos, videos, documents — anything.\n"
                                            "They appear on your iPhone instantly.",
                                vexpand=True)
        choose = Gtk.Button(label="Choose Files…", halign=Gtk.Align.CENTER)
        choose.add_css_class("pill")
        choose.add_css_class("suggested-action")
        choose.connect("clicked", lambda *_: self.choose_files())
        status.set_child(choose)
        empty.append(status)
        self.stack.add_named(empty, "empty")

        listpage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        top = Gtk.Box(spacing=12)
        self.summary = Gtk.Label(xalign=0, hexpand=True)
        self.summary.add_css_class("title-4")
        clear = Gtk.Button(label="Clear All")
        clear.add_css_class("flat")
        clear.connect("clicked", self._on_clear)
        top.append(self.summary)
        top.append(clear)
        listpage.append(top)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.listbox.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.listbox)
        listpage.append(scroller)

        hint = Gtk.Label(label="Drop more files anywhere in this window")
        hint.add_css_class("dim-label")
        hint.add_css_class("dropzone")
        hint.add_css_class("drop-hint")
        listpage.append(hint)
        self.stack.add_named(listpage, "list")

        # ---- right: QR code card ----
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.START)
        side.add_css_class("side-card")
        side.set_size_request(280, -1)
        title = Gtk.Label(label="Scan with iPhone Camera")
        title.add_css_class("title-4")
        side.append(title)

        frame = Gtk.Box(halign=Gtk.Align.CENTER)
        frame.add_css_class("qr-frame")
        self.qr = QRCodeView(220)
        frame.append(self.qr)
        side.append(frame)

        self.url_label = Gtk.Label(selectable=True, wrap=True, justify=Gtk.Justification.CENTER)
        self.url_label.add_css_class("caption")
        self.url_label.add_css_class("monospace")
        side.append(self.url_label)

        copy = Gtk.Button(child=Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copy Link"),
                          halign=Gtk.Align.CENTER)
        copy.add_css_class("pill")
        copy.connect("clicked", self._on_copy)
        side.append(copy)

        note = Gtk.Label(label="Your iPhone and this PC must be on the same Wi-Fi network. "
                               "Files sent from the iPhone go to Downloads › From iPhone.",
                         wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=30)
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        side.append(note)
        self.root.append(side)

        # ---- drag and drop ----
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("enter", self._on_drag_enter)
        target.connect("leave", self._on_drag_leave)
        target.connect("drop", self._on_drop)
        self.root.add_controller(target)

        self.refresh()
        self._update_url()
        GLib.timeout_add_seconds(5, self._update_url)  # follow Wi-Fi network changes

    # ---- actions ----
    def add_paths(self, paths):
        added, missing = 0, 0
        for p in paths:
            try:
                added += self.server.files.add(p)
            except (FileNotFoundError, OSError):
                missing += 1
        self.refresh()
        if added:
            self.toast(f"Sharing {added} new file{'s' if added != 1 else ''}")
        elif missing:
            self.toast("Couldn't read the dropped item")

    def choose_files(self):
        dialog = Gtk.FileDialog(title="Choose files to send to your iPhone")
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # cancelled
        self.add_paths([f.get_path() for f in files if f.get_path()])

    def _on_drag_enter(self, *_):
        self.root.add_css_class("drop-hover")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, *_):
        self.root.remove_css_class("drop-hover")

    def _on_drop(self, _target, value, _x, _y):
        self.root.remove_css_class("drop-hover")
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        self.add_paths(paths)
        return True

    def _on_clear(self, *_):
        self.server.files.clear()
        self.refresh()

    def _on_copy(self, *_):
        self.get_clipboard().set(self._url or "")
        self.toast("Link copied")

    def toast(self, text, button=None, callback=None):
        if os.environ.get("WIFIDROP_SCREENSHOT"):
            return  # keep debug screenshots clean
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=3)
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(t)

    # ---- view updates ----
    def refresh(self):
        snap = self.server.files.snapshot()
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        for fid, name, path, size in snap:
            row = Adw.ActionRow(title=name, subtitle=human(size), use_markup=False)
            ctype, _ = Gio.content_type_guess(name, None)
            row.add_prefix(Gtk.Image(gicon=Gio.content_type_get_symbolic_icon(ctype)))
            rm = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Stop sharing")
            rm.add_css_class("flat")
            rm.connect("clicked", self._on_remove, fid)
            row.add_suffix(rm)
            self.listbox.append(row)
        if snap:
            total = human(sum(s for *_, s in snap))
            self.summary.set_label(f"{len(snap)} file{'s' if len(snap) != 1 else ''} · {total}")
            self.stack.set_visible_child_name("list")
        else:
            self.stack.set_visible_child_name("empty")

    def _on_remove(self, _btn, fid):
        self.server.files.remove(fid)
        self.refresh()

    def _update_url(self):
        url = self.server.url()
        if url != self._url:
            self._url = url
            self.qr.set_text(url)
            self.url_label.set_label(url)
        return GLib.SOURCE_CONTINUE

    def on_server_event(self, kind, name, detail):
        if kind == "download":
            self.toast(f"iPhone is downloading {name}")
        elif kind == "zip":
            self.toast(f"iPhone is downloading {name} as ZIP")
        elif kind == "received":
            self.toast(f"Received {name}", "Show",
                       lambda: Gtk.FileLauncher(file=Gio.File.new_for_path(detail))
                       .open_containing_folder(self, None, None))
            if not self.is_active():
                n = Gio.Notification.new("File received from iPhone")
                n.set_body(name)
                self.get_application().send_notification(None, n)
        return GLib.SOURCE_REMOVE


class DropApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.server = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Wi-Fi Drop")
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS) if hasattr(provider, "load_from_string") \
            else provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.server = DropServer(on_event=lambda *a: GLib.idle_add(self._dispatch, *a))
        self.server.start()

        for name, cb in (("open-received", self._open_received), ("about", self._about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])
        self.set_accels_for_action("window.close", ["<Ctrl>w"])

    def do_shutdown(self):
        if self.server:
            self.server.stop()
        Adw.Application.do_shutdown(self)

    def _dispatch(self, kind, name, detail):
        if self.window:
            self.window.on_server_event(kind, name, detail)
        return GLib.SOURCE_REMOVE

    def _ensure_window(self):
        if not self.window:
            self.window = DropWindow(self, self.server)
            shot = os.environ.get("WIFIDROP_SCREENSHOT")
            if shot:
                GLib.timeout_add(1200, self._screenshot, shot)
        self.window.present()

    def do_activate(self):
        self._ensure_window()

    def do_open(self, files, _n, _hint):
        self._ensure_window()
        self.window.add_paths([f.get_path() for f in files if f.get_path()])

    def _open_received(self, *_):
        folder = self.server.receive_dir
        folder.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher(file=Gio.File.new_for_path(str(folder))).launch(self.window, None, None)

    def _about(self, *_):
        kwargs = dict(application_name="Wi-Fi Drop", application_icon="wifi-drop",
                      version=__version__, developer_name="Rabee Qasem",
                      comments="Send files from Linux to your iPhone over Wi-Fi — no app needed on the phone.",
                      license_type=Gtk.License.MIT_X11)
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kwargs).present(self.window)
        else:
            Adw.AboutWindow(transient_for=self.window, **kwargs).present()

    def _screenshot(self, path):
        """Debug helper: WIFIDROP_SCREENSHOT=/path.png renders the window to a PNG and quits."""
        win = self.window
        paintable = Gtk.WidgetPaintable.new(win)
        w, h = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is not None:
            win.get_native().get_renderer().render_texture(node, None).save_to_png(path)
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv=None):
    return DropApp().run(argv if argv is not None else sys.argv)
