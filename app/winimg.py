# -*- coding: utf-8 -*-
"""Windows GDI+ 图片解码（零第三方依赖，仅 ctypes/stdlib）。

Tk 的 PhotoImage 原生只支持 PNG / GIF / PPM / PGM，不支持 JPEG。
本模块通过 Windows 自带 GDI+ 解码 JPEG（及其他 GDI+ 可识别格式），
返回 (width, height, rgb_bytes)，供上层转成 PPM 喂给 PhotoImage。
"""
import ctypes
import ctypes.wintypes as wt

# ---- GDI+ 所需结构 ----
class _StartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", wt.UINT),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wt.BOOL),
                ("SuppressExternalCodecs", wt.BOOL)]


class _BitmapData(ctypes.Structure):
    _fields_ = [("Width", wt.UINT),
                ("Height", wt.UINT),
                ("Stride", ctypes.c_int),
                ("PixelFormat", wt.UINT),
                ("Scan0", ctypes.c_void_p),
                ("Reserved", ctypes.c_size_t)]


# PixelFormat32bppARGB（内存顺序 BGRA）
_FMT_ARGB32 = 0x0026200a
# ImageLockModeRead
_LOCK_READ = 1

_gdi = None
_token = None
_ref = 0


def _ensure():
    """惰性初始化 GDI+，引用计数保护。"""
    global _gdi, _token, _ref
    if _gdi is not None:
        _ref += 1
        return
    g = ctypes.WinDLL("gdiplus")
    g.GdiplusStartup.restype = wt.UINT
    g.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                 ctypes.POINTER(_StartupInput), ctypes.c_void_p]
    g.GdiplusShutdown.argtypes = [ctypes.c_size_t]
    g.GdipCreateBitmapFromFile.restype = wt.UINT
    g.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p,
                                           ctypes.POINTER(ctypes.c_void_p)]
    g.GdipGetImageWidth.restype = wt.UINT
    g.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.UINT)]
    g.GdipGetImageHeight.restype = wt.UINT
    g.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.UINT)]
    g.GdipBitmapLockBits.restype = wt.UINT
    g.GdipBitmapLockBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
                                     wt.UINT, ctypes.POINTER(_BitmapData)]
    g.GdipBitmapUnlockBits.restype = wt.UINT
    g.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BitmapData)]
    g.GdipDisposeImage.restype = wt.UINT
    g.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    tok = ctypes.c_size_t()
    si = _StartupInput(1, None, False, False)
    st = g.GdiplusStartup(ctypes.byref(tok), ctypes.byref(si), None)
    if st != 0:
        raise OSError("GDI+ 启动失败（status=%d）" % st)
    _gdi = g
    _token = tok
    _ref = 1


def _release():
    global _ref
    if _ref == 0:
        return
    _ref -= 1
    if _ref == 0:
        global _gdi, _token
        try:
            _gdi.GdiplusShutdown(_token)
        finally:
            _gdi = None
            _token = None


def decode_rgb(path):
    """解码图片文件为 (width, height, rgb_bytes)。失败抛异常。"""
    _ensure()
    try:
        bmp = ctypes.c_void_p()
        st = _gdi.GdipCreateBitmapFromFile(path, ctypes.byref(bmp))
        if st != 0 or not bmp.value:
            raise ValueError("无法解码图片（status=%d）" % st)
        try:
            w, h = wt.UINT(), wt.UINT()
            _gdi.GdipGetImageWidth(bmp, ctypes.byref(w))
            _gdi.GdipGetImageHeight(bmp, ctypes.byref(h))
            if w.value < 1 or h.value < 1:
                raise ValueError("图片尺寸无效")
            bd = _BitmapData()
            rect = (ctypes.c_int * 4)(0, 0, w.value, h.value)
            st = _gdi.GdipBitmapLockBits(bmp, rect, _LOCK_READ, _FMT_ARGB32,
                                         ctypes.byref(bd))
            if st != 0 or not bd.Scan0:
                raise ValueError("读取像素失败（status=%d）" % st)
            try:
                buf = ctypes.string_at(bd.Scan0, abs(bd.Stride) * h.value)
            finally:
                _gdi.GdipBitmapUnlockBits(bmp, ctypes.byref(bd))
            ww, hh = w.value, h.value
            stride = bd.Stride
            out = bytearray(ww * hh * 3)
            pos = 0
            for y in range(hh):
                row = y * stride
                x4 = 0
                for x in range(ww):
                    out[pos] = buf[row + x4 + 2]      # R
                    out[pos + 1] = buf[row + x4 + 1]  # G
                    out[pos + 2] = buf[row + x4]      # B
                    pos += 3
                    x4 += 4
            return ww, hh, bytes(out)
        finally:
            _gdi.GdipDisposeImage(bmp)
    finally:
        _release()


def fit_rgb(w, h, rgb, maxdim=800):
    """最大边超过 maxdim 时最近邻降采样；返回 (w, h, rgb)。"""
    if max(w, h) <= maxdim:
        return w, h, rgb
    k = max(1, int(max(w, h) // maxdim) + (1 if max(w, h) % maxdim else 0))
    nw = max(1, w // k)
    nh = max(1, h // k)
    out = bytearray(nw * nh * 3)
    pos = 0
    for y in range(nh):
        sy = min((y * k) * w * 3, (h - 1) * w * 3)
        for x in range(nw):
            sx = sy + min(x * k, w - 1) * 3
            out[pos] = rgb[sx]
            out[pos + 1] = rgb[sx + 1]
            out[pos + 2] = rgb[sx + 2]
            pos += 3
    return nw, nh, bytes(out)
