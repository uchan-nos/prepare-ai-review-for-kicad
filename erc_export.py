"""
Copyright (c) 2026 Kota UCHIDA

KiCad カスタムネットリストエクスポーター

動作:
  1. 中間ネットリスト (%I) を解析して回路図ファイルのパスを取得
  2. 中間ネットリストをタイムスタンプ付きでプロジェクトディレクトリに保存
  3. kicad-cli sch erc を実行して ERC 結果を JSON で保存
  4. 中間ネットリストを AI レビュー用形式に変換して .review.txt で保存
  5. 実行ログをプロジェクトディレクトリに逐次書き出し（tail -f で追跡可）
  Windows 環境では ERC 実行中に経過秒数を表示する。
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime


# ---------- logging ----------

_FALLBACK_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'erc_export_error.log')
_log_file = None


def _open_log(path):
    global _log_file
    if _log_file:
        _log_file.close()
    _log_file = open(path, 'w', encoding='utf-8')


def _log(msg):
    _log_file.write(msg + '\n')
    _log_file.flush()
    print(msg)


def _fatal(msg):
    _log(msg)
    _log_file.close()
    sys.exit(1)


# ---------- Windows プログレス表示付きコマンド実行 ----------
#
# CreateWindowExW で作ったカスタムウィンドウは、何らかの理由でユーザーに見えない。
# MessageBoxW は内部で C コードのメッセージループを持ち GIL 解放中でも動作するため、
# バックグラウンドスレッドから呼び出しても描画が保証される。
# ERC 完了後は PostMessageW(WM_COMMAND, IDOK) でプログラム的に閉じる。


# DPI 認識を有効にしないと、Windows がビットマップ拡大でスケーリングするため
# (DPI 仮想化) High DPI 環境で MessageBox の文字が滲む。
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def _enable_dpi_awareness():
    """プロセスを DPI 認識にする。適用したモード名を返す（適用できなければ None）。

    ウィンドウを 1 つも作る前に呼ぶこと。新しい API から順に試し、
    古い Windows では順にフォールバックする。
    既にマニフェスト等で設定済みの場合は各 API が失敗するが、実害はない。
    """
    if sys.platform != 'win32':
        return None

    # Windows 10 1703 以降
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return 'per-monitor-v2'
    except Exception:
        pass

    # Windows 8.1 以降（shcore.dll）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(
                _PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return 'per-monitor'
    except Exception:
        pass

    # Vista 以降（システム DPI のみ）
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return 'system'
    except Exception:
        pass

    return None


def _run_cmd_with_progress(cmd, title='処理中...'):
    """コマンドを実行しながら MessageBoxW で進捗を表示する。
    Windows 以外は通常の subprocess.run と同等。
    """
    if sys.platform != 'win32':
        return subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        return _run_cmd_with_progress_impl(cmd, title)
    except Exception:
        import traceback
        try:
            with open(_FALLBACK_LOG, 'a', encoding='utf-8') as f:
                f.write('=== progress window exception ===\n')
                f.write(traceback.format_exc())
        except Exception:
            pass
        return subprocess.run(cmd, capture_output=True, text=True)


def _run_cmd_with_progress_impl(cmd, title):
    # MessageBox を作る前に呼ぶ必要がある
    _log(f'DPI awareness: {_enable_dpi_awareness() or "適用できず（既定のまま）"}')

    user32   = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    _H       = ctypes.wintypes.HANDLE
    _LPCWSTR = ctypes.wintypes.LPCWSTR
    user32.MessageBoxW.restype             = ctypes.c_int
    user32.FindWindowW.restype             = ctypes.wintypes.HWND
    user32.FindWindowW.argtypes            = [_LPCWSTR, _LPCWSTR]
    user32.FindWindowExW.restype           = ctypes.wintypes.HWND
    user32.FindWindowExW.argtypes          = [_H, _H, _LPCWSTR, _LPCWSTR]
    user32.GetDlgItem.restype              = ctypes.wintypes.HWND
    user32.GetDlgItem.argtypes             = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.GetWindowTextLengthW.restype    = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes   = [ctypes.wintypes.HWND]
    user32.SetWindowTextW.restype          = ctypes.wintypes.BOOL
    user32.SetWindowTextW.argtypes         = [_H, _LPCWSTR]
    user32.ShowWindow.restype              = ctypes.wintypes.BOOL
    user32.ShowWindow.argtypes             = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.SetWindowsHookExW.restype       = _H
    user32.SetWindowsHookExW.argtypes      = [ctypes.c_int, ctypes.c_void_p,
                                              _H, ctypes.wintypes.DWORD]
    user32.UnhookWindowsHookEx.restype     = ctypes.wintypes.BOOL
    user32.UnhookWindowsHookEx.argtypes    = [_H]
    user32.CallNextHookEx.restype          = ctypes.c_long
    user32.CallNextHookEx.argtypes         = [_H, ctypes.c_int,
                                              ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.PostMessageW.restype            = ctypes.wintypes.BOOL
    user32.PostMessageW.argtypes           = [_H, ctypes.wintypes.UINT,
                                              ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    kernel32.Sleep.restype                 = None
    kernel32.GetCurrentThreadId.restype    = ctypes.wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes   = []

    MB_ICONINFORMATION = 0x00000040
    MB_APPLMODAL       = 0x00000000
    MB_OKCANCEL        = 0x00000001
    IDOK               = 1
    IDCANCEL           = 2
    SW_HIDE            = 0
    WM_COMMAND         = 0x0111
    WH_CBT             = 5
    HCBT_ACTIVATE      = 5

    def _fmt(elapsed):
        return f'ERC 実行中...\n\n経過: {elapsed} 秒'

    cancel_event = threading.Event()

    def show_mb():
        # WH_CBT フックで HCBT_ACTIVATE（WM_PAINT より前）を捕捉し、
        # 初回描画前に OK ボタンを非表示にする → ちらつきなし
        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        )
        hook_h = [None]

        def cbt_proc(nCode, wParam, lParam):
            if nCode == HCBT_ACTIVATE:
                hwnd_ok_btn = user32.GetDlgItem(wParam, IDOK)
                if hwnd_ok_btn:
                    user32.ShowWindow(hwnd_ok_btn, SW_HIDE)
                if hook_h[0]:
                    user32.UnhookWindowsHookEx(hook_h[0])
                    hook_h[0] = None
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        hook_fn = HOOKPROC(cbt_proc)   # GC されないよう参照を保持
        hook_h[0] = user32.SetWindowsHookExW(WH_CBT, hook_fn,
                                             None, kernel32.GetCurrentThreadId())

        retval = user32.MessageBoxW(0, _fmt(0), title,
                                    MB_ICONINFORMATION | MB_APPLMODAL | MB_OKCANCEL)
        if hook_h[0]:   # フックが発火しなかった場合のフォールバック解除
            user32.UnhookWindowsHookEx(hook_h[0])
        if retval == IDCANCEL:
            cancel_event.set()

    mb_thread = threading.Thread(target=show_mb, daemon=True)
    mb_thread.start()
    kernel32.Sleep(300)   # MessageBoxW が描画されるまで待つ (ctypes call = GIL 解放)

    # ERC を非同期で開始し、ポーリングループで経過秒数を更新する
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    start_time = time.time()
    hwnd_mb     = None
    hwnd_static = None

    while proc.poll() is None:
        # キャンセルボタンが押されたら ERC を強制終了して即リターン
        if cancel_event.is_set():
            proc.kill()
            proc.wait()
            return subprocess.CompletedProcess(cmd, -1, '', 'キャンセルされました')

        elapsed = int(time.time() - start_time)

        if hwnd_mb is None:
            hwnd_mb = user32.FindWindowW(None, title)

        # テキストを持つ最初の Static 子ウィンドウを探す（アイコン Static はテキスト長 0）
        if hwnd_mb and hwnd_static is None:
            h = user32.FindWindowExW(hwnd_mb, None, 'Static', None)
            while h:
                if user32.GetWindowTextLengthW(h) > 0:
                    hwnd_static = h
                    break
                h = user32.FindWindowExW(hwnd_mb, h, 'Static', None)

        if hwnd_static:
            user32.SetWindowTextW(hwnd_static, _fmt(elapsed))

        kernel32.Sleep(1000)   # 1 秒ごとに更新（ctypes call = GIL 解放）

    # ERC 完了（キャンセルなし）→ ダイアログを自動クローズ
    stdout, stderr = proc.communicate()
    if hwnd_mb is None:
        hwnd_mb = user32.FindWindowW(None, title)
    if hwnd_mb:
        user32.PostMessageW(hwnd_mb, WM_COMMAND, IDOK, 0)
    mb_thread.join(timeout=2)

    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# ---------- パス正規化 ----------

def _normalize_sch_path(sch_file, input_net):
    """Linux パス (/home/...) を WSL UNC パス (\\\\wsl.localhost\\...) に変換する。"""
    if sys.platform != 'win32' or not sch_file.startswith('/'):
        return sch_file
    norm = input_net.replace('/', '\\')
    parts = norm.lstrip('\\').split('\\')
    if len(parts) >= 2 and parts[0].lower() == 'wsl.localhost':
        prefix = '\\\\' + '\\'.join(parts[:2])
        return prefix + sch_file.replace('/', '\\')
    return sch_file


# ---------- kicad-cli の探索 ----------

def find_kicad_cli():
    exe = 'kicad-cli.exe' if sys.platform == 'win32' else 'kicad-cli'
    sibling = os.path.join(os.path.dirname(sys.executable), exe)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(exe)


# ---------- ネットリスト解析 ----------

def _find_field(comp, name):
    """<comp> 直下の <fields><field name="..."> を大文字小文字を無視して探す。"""
    for field in comp.findall('fields/field'):
        if (field.get('name', '') or '').lower() == name.lower():
            return (field.text or '').strip()
    return ''


def parse_netlist(root_elem):
    """KiCad XML ネットリストを解析して (components, pins_by_comp, nets, power_nets) を返す。

    components:   ref -> {value, libpart, lib, description, sheet,
                          purpose, manufacturer, manufacturer_pn, footprint}
    pins_by_comp: ref -> {pin -> {net, pintype, pinfunction}}
    nets:         net_name -> [{ref, pin, pintype, pinfunction}]
    power_nets:   power_out ノードを持つネット名の集合
    """
    components = {}
    for comp in root_elem.findall('components/comp'):
        ref = comp.get('ref', '')
        value = comp.findtext('value') or ''
        libsrc = comp.find('libsource')
        # lib（ライブラリ名）と description は外部インターフェースの判定に使う
        libpart = libsrc.get('part', '') if libsrc is not None else ''
        lib = libsrc.get('lib', '') if libsrc is not None else ''
        description = libsrc.get('description', '') if libsrc is not None else ''
        sheetpath = comp.find('sheetpath')
        sheet = sheetpath.get('names', '/') if sheetpath is not None else '/'
        components[ref] = {
            'value': value,
            'libpart': libpart,
            'lib': lib,
            'description': description,
            'sheet': sheet,
            'purpose': _find_field(comp, 'Purpose'),
            'manufacturer': _find_field(comp, 'Manufacturer'),
            'manufacturer_pn': _find_field(comp, 'Manufacturer PN'),
            'footprint': (comp.findtext('footprint') or '').strip(),
        }

    pins_by_comp = {ref: {} for ref in components}
    nets = {}
    power_nets = set()

    for net_elem in root_elem.findall('nets/net'):
        net_name = net_elem.get('name', '')
        nodes = []
        has_power_out = False
        for node in net_elem.findall('node'):
            ref = node.get('ref', '')
            pin = node.get('pin', '')
            pintype = node.get('pintype', '')
            pinfunction = node.get('pinfunction', '')
            nodes.append({'ref': ref, 'pin': pin,
                          'pintype': pintype, 'pinfunction': pinfunction})
            if ref not in pins_by_comp:
                pins_by_comp[ref] = {}
            pins_by_comp[ref][pin] = {
                'net': net_name, 'pintype': pintype, 'pinfunction': pinfunction}
            if pintype == 'power_out':
                has_power_out = True
        nets[net_name] = nodes
        if has_power_out:
            power_nets.add(net_name)

    return components, pins_by_comp, nets, power_nets


# ---------- 設計メモ（回路図テキスト）の抽出 ----------
#
# 中間ネットリストに回路図のテキストは入らないので .kicad_sch を直接読む。
# 見出しの無いテキストは拾わない。位置で近くの部品に紐づく注記であり、
# 位置を落として並べると、どの部品の話か分からないまま AI に渡ってしまう。

_DESIGN_NOTE_HEADINGS = ('design notes', '設計メモ', '設計ノート')

# 見出しの直後はコロン（半角・全角）か改行。「設計メモは別紙」のような
# 通常の文を見出しと誤認しないための条件。
_NOTE_HEADING_END_RE = re.compile(r'[ \t]*[:：][ \t]*|[ \t]*\n')

# .kicad_sch のテキスト要素。text は素のテキスト、text_box は枠付きテキスト
_SCH_TEXT_RE = re.compile(r'\((?:text|text_box)\s+"((?:[^"\\]|\\.)*)"')

_SCH_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r'}


def _unescape_sch_string(text):
    """.kicad_sch の文字列リテラルのエスケープを戻す（改行は \n で格納される）。"""
    return re.sub(r'\\(.)', lambda m: _SCH_ESCAPES.get(m.group(1), m.group(1)), text)


def _design_note_body(text):
    """設計メモなら見出しを除いた本文を返す。設計メモでなければ None。"""
    text = text.lstrip()
    lowered = text.lower()
    for heading in _DESIGN_NOTE_HEADINGS:
        if not lowered.startswith(heading):
            continue
        rest = text[len(heading):]
        end = _NOTE_HEADING_END_RE.match(rest)
        if end:
            return rest[end.end():].strip() or None
    return None


def parse_design_notes(sch_text):
    """.kicad_sch の内容から設計メモの本文を、書かれている順に返す。"""
    notes = []
    for raw in _SCH_TEXT_RE.findall(sch_text):
        body = _design_note_body(_unescape_sch_string(raw))
        if body:
            notes.append(body)
    return notes


def read_design_notes(root_elem, sch_file, on_error=None):
    """回路図ファイル（サブシート含む）から設計メモを集める。

    サブシートのファイル名は中間ネットリストの design/sheet に入っている。
    読めなかったファイルは on_error(path, exc) で報告する。報告しないと、
    「回路図に見出しを書き忘れた」と「回路図を読めなかった」が
    どちらも 0 件になり、書いたはずのメモが消えたことに気づけない。
    """
    proj_dir = os.path.dirname(sch_file)
    paths = [sch_file]
    for sheet in root_elem.findall('design/sheet'):
        name = (sheet.findtext('title_block/source') or '').strip()
        path = os.path.join(proj_dir, name) if name else ''
        if path and path not in paths:
            paths.append(path)

    notes = []
    for path in paths:
        try:
            with open(path, encoding='utf-8') as f:
                notes.extend(parse_design_notes(f.read()))
        except OSError as exc:
            if on_error:
                on_error(path, exc)
    return notes


# ---------- 異常検出 ----------

def _is_positive_supply(net_name):
    n = net_name.upper().lstrip('/')
    return n.startswith('+') or any(
        n.startswith(p) for p in ('VCC', 'VDD', 'AVCC', 'AVDD'))


def _is_gnd(net_name):
    n = net_name.upper().lstrip('/')
    return n in ('GND', 'AGND', 'DGND', 'PGND') or n.startswith('GND')


_ZERO_OHM_RE = re.compile(r'^0+(?:[.,]0+)?\s*(?:R0*|OHM|Ω|R)?$', re.IGNORECASE)


def _is_zero_ohm(value):
    """"0" / "0R" / "0R0" / "0Ω" などのゼロオーム表記か。"""
    return bool(_ZERO_OHM_RE.match(value.strip()))


def _two_pin_power_anomaly(ref, value, net_a, net_b, gnd_nets):
    """電源ネット間に入った 2 端子部品を判定し、(severity, msg) か None を返す。

    デカップリングコンデンサ（電源-GND）や直列フェライト・ヒューズ（電源-電源）は
    正常な構成なので検出しない。DC 短絡になる組み合わせだけを拾う。
    net_a と net_b は異なるネットであること（同一ネットは呼び出し側で判定済み）。
    """
    kind = ref[:1].upper()
    a_gnd, b_gnd = net_a in gnd_nets, net_b in gnd_nets
    to_gnd = a_gnd != b_gnd      # 片側だけ GND = 電源を GND へ落とすシャント接続
    nets_str = f'{net_a} / {net_b}'

    if to_gnd and kind in ('L', 'F'):
        part = 'インダクタ' if kind == 'L' else 'ヒューズ'
        return ('WARNING',
                f'{ref}({value}) {part}が電源-GND 間 [{nets_str}]  [DC 短絡]')

    if to_gnd and kind == 'R' and _is_zero_ohm(value):
        return ('WARNING',
                f'{ref}({value}) 0Ω 抵抗が電源-GND 間 [{nets_str}]  [DC 短絡]')

    # 2 つの電源レールを直接またぐコンデンサ（GND 同士の接続は除く）
    if not to_gnd and kind == 'C' and not (a_gnd and b_gnd):
        return ('WARNING', f'{ref}({value}) コンデンサが 2 つの電源間 [{nets_str}]')

    return None


def detect_anomalies(components, pins_by_comp, power_nets):
    gnd_nets = {n for n in power_nets if _is_gnd(n)}
    pos_supply_nets = {n for n in power_nets if _is_positive_supply(n)}
    anomalies = []

    for ref, pins in pins_by_comp.items():
        for pin_num, info in pins.items():
            net = info['net']
            pf = info.get('pinfunction', '').upper()
            pt = info.get('pintype', '')

            # V+ 系ピンが GND ネットに接続
            if (any(pf.startswith(s) for s in ('V+', 'VCC', 'VDD', 'AVCC', 'AVDD'))
                    and net in gnd_nets):
                anomalies.append(('CRITICAL',
                    f'{ref} pin{pin_num}({info["pinfunction"]}) → {net}  [電源逆接続]'))

            # V- 系ピンが正電源ネットに接続
            if (any(pf.startswith(s) for s in ('V-', 'VSS'))
                    and net in pos_supply_nets):
                anomalies.append(('CRITICAL',
                    f'{ref} pin{pin_num}({info["pinfunction"]}) → {net}  [電源逆接続]'))

            # 出力ピンが電源ネットに直結
            if pt == 'output' and net in power_nets:
                anomalies.append(('CRITICAL',
                    f'{ref} pin{pin_num} (output) → {net}  [出力が電源ネット直結]'))

        # 2 端子パッシブ部品の接続チェック
        if len(pins) == 2 and ref[:1].upper() in ('R', 'C', 'L', 'D', 'F'):
            pin_vals = list(pins.values())
            net_a, net_b = pin_vals[0]['net'], pin_vals[1]['net']
            val = components.get(ref, {}).get('value', '')
            if net_a == net_b:
                # 電源・信号を問わず、両端が同じネットなら部品は短絡されて機能しない
                anomalies.append(('WARNING',
                    f'{ref}({val}) 両端が同一ネット [{net_a}]  [部品が短絡]'))
            elif all(p['net'] in power_nets for p in pin_vals):
                anomaly = _two_pin_power_anomaly(ref, val, net_a, net_b, gnd_nets)
                if anomaly:
                    anomalies.append(anomaly)

    return anomalies


# ---------- AI レビュー形式へ変換 ----------

def _ref_sort_key(ref):
    m = re.match(r'([A-Za-z_]+)(\d+)', ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


_PINTYPE_SHORT = {
    'power_in': 'pwr', 'power_out': 'pwr', 'input': 'in',
    'output': 'out', 'bidirectional': 'bi', 'tristate': 'tri',
    'passive': 'pas', 'unspecified': '?',
}


def _pt_short(pintype):
    return _PINTYPE_SHORT.get(pintype, pintype[:3] if pintype else '?')


def _pin_num_key(item):
    """ピン番号順（数値ピンが先、非数値ピンは後ろ）に並べるためのキー。"""
    pin = item[0]
    try:
        return (0, int(pin), '')
    except ValueError:
        return (1, 0, pin)


# 外部インターフェースの判定。lib が Connector 系なら ref を問わず外部とする
# （KiCad 公式 Connector ライブラリの中身はほぼ全部が外部境界）。カスタム
# ライブラリは lib から判断できないため、ref が J/P/TP のものに限って部品名・
# description のキーワードで判定し、キーワード一致の誤判定範囲を ref で絞る。
# 誤判定しても INTERNAL COMPONENTS 側に出るだけで、情報そのものは落ちない。
_EXTERNAL_REF_RE = re.compile(r'^(?:J|P|TP)\d')
_CONNECTOR_LIB_RE = re.compile(r'connector', re.IGNORECASE)
_CONNECTOR_KEYWORDS = ('conn', 'header', 'jack', 'socket', 'receptacle',
                       'plug', 'terminal', 'testpoint', 'test point')


def _is_external_interface(ref, comp):
    """回路と外部との境界（コネクタ・テストポイント類）か。"""
    if ref.startswith('#'):      # 電源シンボル (#PWR, #FLG)
        return False
    if _CONNECTOR_LIB_RE.search(comp.get('lib', '')):
        return True
    if not _EXTERNAL_REF_RE.match(ref):
        return False
    text = ' '.join((comp.get('libpart', ''), comp.get('value', ''),
                     comp.get('description', ''))).lower()
    return any(kw in text for kw in _CONNECTOR_KEYWORDS)


def _pin_display_name(pinfunction, pin):
    """表示用のピン名。末尾の冗長な "_ピン番号" を落とす。

    KiCad 10 のネットリストは pinfunction を「ピン名_ピン番号」で出す
    （シンボル側のピン名が RX / Pin_1 でも、ネットリストでは RX_1 / Pin_1_1）。
    ピン行は pinN で始まるので、末尾の番号の繰り返しは何の情報も持たない。
    番号が一致しない末尾（pin7 の "FB_3" など）は名前の一部なので残す。
    """
    suffix = f'_{pin.strip()}'
    if pinfunction.endswith(suffix) and len(pinfunction) > len(suffix):
        return pinfunction[:-len(suffix)]
    return pinfunction


# "Pin_1" / "Pad2" のようにピン番号を言い換えただけのピン名を見分ける
_PIN_NUMBER_ALIAS_RE = re.compile(r'^(?:pin|pad|p)?[_\-\s]?0*(\d+)$', re.IGNORECASE)


def _is_pin_number_restated(pinfunction, pin):
    m = _PIN_NUMBER_ALIAS_RE.match(pinfunction)
    return bool(m) and m.group(1) == pin.strip().lstrip('0')


def _informative_pin_name(pinfunction, pin):
    """情報を持つピン名だけを返す。空、またはピン番号の言い換えなら空文字列。"""
    pf = _pin_display_name((pinfunction or '').strip(), pin)
    return '' if not pf or _is_pin_number_restated(pf, pin) else pf


def _pin_annotation(pin, info):
    """ピン行に付ける "(ピン名,ピンタイプ)"。語ることが無ければ空文字列。

    括弧の有無を情報の有無に一致させる。汎用ピンヘッダは pinfunction が
    "Pin_1"、pintype が passive で、どちらもピン番号以上のことを言わないので
    注記ごと省いて pin1=NET と出す。括弧が無いピンは passive（または
    unspecified）だと読み手が復元できる。

    括弧を出すときは必ず 2 項目そろえる。(K) や (in) のような片方だけの形を
    混ぜると、読み手が括弧を見るたびに「今どちらが入っているのか」を
    判定することになり、括弧 1 個分の節約に見合わない。
    """
    name = _informative_pin_name(info.get('pinfunction'), pin)
    pintype = info.get('pintype', '')
    if not name and pintype in ('', 'passive', 'unspecified'):
        return ''
    return f'({name},{_pt_short(pintype)})'


def _erc_violations(erc_data):
    """ERC 結果 JSON から違反リストを取り出す。

    kicad-cli sch erc --format json は違反をシートごとの sheets[].violations に
    格納する。トップレベルの violations も念のため拾う。
    """
    if not erc_data:
        return []
    violations = list(erc_data.get('violations') or [])
    for sheet in erc_data.get('sheets') or []:
        violations.extend(sheet.get('violations') or [])
    return violations


def _count_violations(erc_data):
    """(error 数, warning 数, 除外数) を返す。除外された違反は件数に含めない。

    KiCad 10.0.4 の出力には excluded フィールドが無く（included_severities で
    kicad-cli 側が事前に絞る）、除外数は常に 0 になる。将来版や他バージョンが
    excluded 付きで出力した場合に過大計上しないための防御。
    """
    violations = _erc_violations(erc_data)
    active = [v for v in violations if not v.get('excluded')]
    n_err  = sum(1 for v in active if v.get('severity') == 'error')
    n_warn = sum(1 for v in active if v.get('severity') == 'warning')
    return n_err, n_warn, len(violations) - len(active)


def format_review(source_file, components, pins_by_comp, nets, power_nets, anomalies,
                  erc_data=None, erc_cancelled=False, design_notes=None):
    gnd_nets = {n for n in power_nets if _is_gnd(n)}
    pos_supply_nets = {n for n in power_nets if _is_positive_supply(n)}

    def pin_flag(info):
        net = info['net']
        pf = info.get('pinfunction', '').upper()
        pt = info.get('pintype', '')
        if pt == 'output' and net in power_nets:
            return ' !!'
        if any(pf.startswith(s) for s in ('V+', 'VCC', 'VDD')) and net in gnd_nets:
            return ' !!'
        if any(pf.startswith(s) for s in ('V-', 'VSS')) and net in pos_supply_nets:
            return ' !!'
        return ''

    proj_name = os.path.splitext(os.path.basename(source_file))[0]
    lines = [
        f'=== AI REVIEW: {proj_name} ===',
        f'source: {source_file}',
        '',
        'このファイルは KiCad 回路図（.kicad_sch）の自動生成レビュー用データです。',
        'AI による回路チェックを目的として、ネットリストと ERC 結果を 1 ファイルにまとめています。',
        '',
        'セクション構成:',
        '  DESIGN NOTES — 回路図にテキストで書かれた設計メモ（"設計メモ:" や "Design Notes:" で始まるテキスト）。回路全体にかかる前提や、部品単位の Purpose に書くには長い記述が入る。回路図作成者が書いた設計意図であり、ツールは検証していない。該当するテキストが無ければこのセクションは出ない。',
        '  ANOMALIES — ネットリスト解析による疑わしい接続の自動検出（人間のレビューが必要）。デカップリングコンデンサ、プルダウン抵抗、直列フェライト等の正常な構成は意図的に検出対象外。ここに出ないことは正常の裏付けではない。',
        '  ERC RESULTS — KiCad の Electrical Rules Check（電気的規則検査）結果 JSON。件数は全シートの合計。回路図側で除外された違反は excluded として別計上。無効化されたチェックがあれば件数の直後に列挙する。',
        '  EXTERNAL INTERFACES — 回路と外部との境界（コネクタ・テストポイント類）とその接続。ピンは番号順に並べ、電源ピンも信号ピンと同じ並びのまま記載する（コネクタは物理的なピン並びが本質のため）。内部部品と外部インターフェースの振り分けは Library Symbol（lib / part / description）と ref からの自動判定であり、誤りうる。部品名を見て境界かどうかを確認すること。',
        '  INTERNAL COMPONENTS — 回路内部の部品一覧とピン接続',
        '  SIGNAL NETS — 信号ネット一覧（電源ネットを除く）。外部インターフェースのピンも含む',
        '',
        '部品の属性（EXTERNAL INTERFACES と INTERNAL COMPONENTS で共通。回路図に設定がある部品のみ 1 行ずつ記載し、未設定なら行ごと省略する。値は KiCad のものそのまま）:',
        '  部品行の Value — 設計上の値・型名',
        '  Purpose — この回路での用途（回路図作成者が記入した設計意図。ツールは検証していないので、接続や定数と食い違わないか確認すること）',
        '  Footprint — 物理的な実装',
        '  Manufacturer — 部品のメーカー名',
        '  Manufacturer PN — 部品のメーカー型番',
        '',
        'ピン行の表記（両セクション共通）: pin<番号>(ピン名,ピンタイプ)=ネット名。括弧の中は必ず「ピン名,ピンタイプ」の 2 項目で、片方だけの形は使わない。ピン名がピン番号の言い換え（Pin_1 等）のときは名前の欄を空にする（例 pin1(,in)=ネット名）。ピン名も型も語ることが無いピン、すなわち名前が番号の言い換えで型が passive / unspecified のピンは、括弧ごと省いて pin1=ネット名 と書く。したがって括弧が無いピンは passive（または unspecified）である。',
        '電源ピン（power_in / power_out）の行も書式は同じで、電源ピンであることは注記の pwr が示す。INTERNAL COMPONENTS ではその部品のピンのうち電源ピンを先に置く。EXTERNAL INTERFACES では並べ替えず、コネクタのピン番号順のまま置く。',
        'ネット名の後ろの !! は、電源の逆接続や出力の電源ネット直結など、ツールが疑わしいと判断した接続を指す（詳細は ANOMALIES）。印が無いことは検査して正常だったことを意味しない。',
        'ピンタイプ略称: pwr=電源, in=入力, out=出力, bi=双方向, tri=3ステート, pas=パッシブ',
        '',
        '== AI REVIEW GUIDANCE ==',
        '',
        'このファイルは、回路図をAIがレビューしやすい形に変換したデータです。',
        '以下の方針でレビューしてください。ユーザーから追加のレビュー指示がある場合は、そちらも考慮してください。',
        '',
        '* 回路が意図どおり動作するか、部品の定格・電圧・電流・電力・信号レベル・電源条件などに問題がないかを確認してください。',
        '* 問題を指摘するときは、可能な限り実際の回路条件を考慮してください。ICの最大定格や電流制限値などと部品定格を単純比較するだけでNGとは判断せず、必要に応じて実際に想定される電圧・電流・電力・ピーク値などを計算または見積もってください。',
        '* 計算や判断に前提条件が必要な場合は、その前提を明示してください。回路情報だけでは判断できない場合は、問題と断定せず「要確認」としてください。',
        '* 「致命的」「発注前に修正必須」などの強い判定は、回路が動作しない、部品破損や定格超過が起こる、または重大な誤動作につながる可能性が十分高い場合に限定してください。',
        '* Value や Manufacturer PN など部品を特定できる情報がある場合は、それを部品選定の評価に利用してください。外部資料を確認できる場合は、できるだけメーカーのデータシートを優先してください。',
        '* DESIGN NOTES には、回路全体の設計意図・前提・仕様・使用条件などが記載されています。まずこれらを読んで設計の前提を把握し、そのうえで実際の接続・部品定数・部品選定が記述と整合しているかを確認してください。',
        '* 部品に記載された Purpose は部品を配置している目的を表します。実際の接続・部品定数が整合しているかを確認してください。',
        '* 部品に記載された Purpose は、その部品をこの回路で使用する目的を表します。DESIGN NOTES と Purpose は回路図作成者が記入したもので正しさは保証されていないため、実際の接続・部品定数・部品選定と照合して検証してください。',
        '* Value、Purpose、Manufacturer PN、Footprint、ERC結果、ネット接続など、ファイルに含まれる情報を相互に関連付けて判断してください。',
        '* ERCやANOMALIESの警告は、その存在だけで回路上の問題とは判断せず、実際の接続や設計意図を確認して評価してください。',
        '* 同じ原因から生じる複数の症状は、できるだけ一つの問題としてまとめてください。指摘件数を増やすことより、独立した問題を正確に特定することを優先してください。',
        '* 問題が見つからない箇所についても、重要な回路ブロックについて確認できたことを必要に応じて示してください。',
        '* このファイルに含まれない情報を推測で補わないでください。不明な情報がレビューに重要なら、その情報が不足していることを明示してください。',
        '* PCBレイアウトを含まない場合、配線長、配置、電流ループ面積、熱経路などPCBレイアウトに依存する問題は断定しないでください。一方、コネクタ、ピンヘッダ、ケーブル、テストポイントへの接続など、回路図およびFootprint情報だけで判断できる物理的・EMI上の設計事項はレビュー対象に含めてください。',
        r'* 数式は LaTeX を使わず、プレーンテキストとして記述してください。Unicode の数学記号は使用して構いません。例えば、\approx は ≈、\pm は ±、\Omega は Ω、\int は ∫ のように表してください。\sqrt{x} は sqrt(x) のように書いてください。分数は \frac は使わず a / b で表してください。掛け算は a × b、単純な累乗は x² や x³ のように書いてください。複雑な式では括弧を使い、LaTeX の数式環境を表す \[...\] や $...$ などは使用しないでください。例：I = C × dV/dt = 44 µF × 3.2 mV/µs ≈ 0.14 A',
        '',
        'レビューでは、重大な問題、要確認事項、改善提案を区別し、それぞれについて「なぜ問題なのか」と「どの程度の確度でそう判断したか」が分かるようにしてください。',
        '',
    ]

    # 設計メモ（回路図に書かれていなければセクションごと出さない）
    if design_notes:
        lines.append('== DESIGN NOTES ==')
        lines.append('\n\n'.join(design_notes))
        lines.append('')

    # 異常一覧
    lines.append('== ANOMALIES ==')
    if anomalies:
        for severity, msg in anomalies:
            lines.append(f'[{severity}] {msg}')
    else:
        lines.append('(none detected)')
    lines.append('')

    # ERC 結果（kicad-cli が出力した JSON をそのまま埋め込む）
    if erc_cancelled:
        lines.append('== ERC RESULTS ==')
        lines.append('KiCad ERC はユーザーによりキャンセルされました。')
        lines.append('')
    elif erc_data is not None:
        n_err, n_warn, n_excl = _count_violations(erc_data)
        ignored = erc_data.get('ignored_checks') or []
        counts = f'{n_err} error(s), {n_warn} warning(s)'
        if n_excl:
            counts += f', {n_excl} excluded'
        if ignored:
            counts += f', {len(ignored)} checks ignored'
        lines.append(f'== ERC RESULTS == ({counts})')
        if ignored:
            # 無効化されたチェックは JSON 末尾に埋もれるため、件数の直後に明示する。
            # これを知らないと「0 error」を検査済みの裏付けと誤解しかねない。
            lines.append(f'無効化された ERC チェック ({len(ignored)} 件) '
                         '— この種類の違反は上の件数に含まれない:')
            for chk in ignored:
                lines.append(f'  - {chk.get("key", "")}: {chk.get("description", "")}')
        lines.append('```json')
        lines.append(json.dumps(erc_data, ensure_ascii=False, indent=2))
        lines.append('```')
        lines.append('')

    external_refs = [ref for ref in components
                     if _is_external_interface(ref, components[ref])]
    is_external = set(external_refs)

    # 外部インターフェース（コネクタ・テストポイント類）
    lines.append('== EXTERNAL INTERFACES == (回路と外部との境界)')
    if external_refs:
        for ref in sorted(external_refs, key=_ref_sort_key):
            comp = components[ref]
            head = f'{ref}  {comp["value"]}'
            if comp.get('libpart') and comp['libpart'] != comp['value']:
                head += f'  ({comp["libpart"]})'
            lines.append('')
            lines.append(head)
            for label, key in (('Purpose', 'purpose'),
                               ('Footprint', 'footprint'),
                               ('Manufacturer', 'manufacturer'),
                               ('Manufacturer PN', 'manufacturer_pn')):
                if comp.get(key):
                    lines.append(f'  {label}: {comp[key]}')
            for pin, info in sorted(pins_by_comp.get(ref, {}).items(),
                                    key=_pin_num_key):
                annotation = _pin_annotation(pin, info)
                lines.append(
                    f'  pin{pin}{annotation}={info["net"]}{pin_flag(info)}')
    else:
        lines.append('(none detected)')
    lines.append('')

    # 内部部品一覧（シート別）。外部インターフェースは専用セクションに出す
    lines.append('== INTERNAL COMPONENTS ==')
    sheets = {}
    for ref in components:
        if ref in is_external:
            continue
        sheets.setdefault(components[ref]['sheet'], []).append(ref)

    for sheet in sorted(sheets):
        lines.append(f'\n[{sheet}]')
        for ref in sorted(sheets[sheet], key=_ref_sort_key):
            if ref.startswith('#'):  # 電源シンボル (#PWR, #FLG 等) は省略
                continue
            comp = components[ref]
            lines.append(f'  {ref}  {comp["value"]}  ({comp["libpart"]})')
            # 属性はフィールド名をラベルにした 1 行 1 項目で出す。略称にすると
            # 凡例を参照しないと読めず、トークンの節約分より読み手の負荷が勝る。
            # Footprint の値は KiCad のまま（解釈・短縮・正規化はしない）。
            for label, key in (('Purpose', 'purpose'),
                               ('Footprint', 'footprint'),
                               ('Manufacturer', 'manufacturer'),
                               ('Manufacturer PN', 'manufacturer_pn')):
                if comp.get(key):
                    lines.append(f'    {label}: {comp[key]}')
            pins = pins_by_comp.get(ref, {})

            # 電源ピンを先に置く。ピン行の書式は他のピンと同じで、
            # 電源ピンであることは注記の pwr が示す
            pwr_pins = sorted(
                [(p, i) for p, i in pins.items()
                 if i.get('pintype') in ('power_in', 'power_out')],
                key=_pin_num_key)
            sig_pins = sorted(
                [(p, i) for p, i in pins.items()
                 if i.get('pintype') not in ('power_in', 'power_out')],
                key=_pin_num_key)

            for pn, info in pwr_pins:
                lines.append(f'    pin{pn}{_pin_annotation(pn, info)}='
                             f'{info["net"]}{pin_flag(info)}')

            for pn, info in sig_pins:
                lines.append(f'    pin{pn}{_pin_annotation(pn, info)}='
                             f'{info["net"]}{pin_flag(info)}')

    lines.append('')

    # 信号ネット一覧（電源ネット除く）
    lines.append('== SIGNAL NETS == (電源ネット除く)')
    for net_name in sorted(nets):
        if net_name in power_nets:
            continue
        nodes = nets[net_name]
        parts = [
            f'{n["ref"]}:{n["pin"]}({_pt_short(n.get("pintype", ""))})'
            for n in nodes
        ]
        lines.append(f'  {net_name}')
        lines.append(f'    {" ".join(parts)}')
    lines.append('')

    return '\n'.join(lines)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description='KiCad ERC エクスポーター')
    parser.add_argument('input_net', metavar='%I',
                        help='KiCad 中間ネットリスト')
    parser.add_argument('output', nargs='?', metavar='%O',
                        help='KiCad 出力パス（未使用）')
    parser.add_argument('--review-only', action='store_true',
                        help='.net / erc_*.json / .net.log を削除して review.md だけ残す')
    args = parser.parse_args()
    input_net = args.input_net
    keep_intermediates = not args.review_only

    _open_log(_FALLBACK_LOG)

    try:
        root_elem = ET.parse(input_net).getroot()
    except ET.ParseError as e:
        _fatal(f'error: failed to parse netlist: {e}')

    source_elem = root_elem.find('design/source')
    if source_elem is None:
        _fatal('error: <design><source> not found in netlist')

    sch_file = _normalize_sch_path(source_elem.text or '', input_net)
    proj_dir = os.path.dirname(sch_file)
    proj_name = os.path.splitext(os.path.basename(sch_file))[0]

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        _fatal('error: kicad-cli not found')

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    ai_review_dir = os.path.join(proj_dir, 'ai-review')
    os.makedirs(ai_review_dir, exist_ok=True)
    net_out    = os.path.join(ai_review_dir, f'{proj_name}_{ts}.net')
    erc_out    = os.path.join(ai_review_dir, f'{proj_name}_erc_{ts}.json')
    review_out = os.path.join(ai_review_dir, f'{proj_name}_{ts}.review.txt')

    try:
        _open_log(net_out + '.log')
        _log(f'sch_file: {sch_file}')
        _log(f'netlist:  {net_out}')

        shutil.copy2(input_net, net_out)
        _log('netlist saved')

        # ERC（Windows ではプログレスバーをメインスレッドで表示しながら実行）
        _log('running ERC...')
        erc_proc = _run_cmd_with_progress(
            [kicad_cli, 'sch', 'erc', '--format', 'json', '--output', erc_out, sch_file],
            title='ERC & Export: ERC 実行中...',
        )
        erc_cancelled = erc_proc.returncode == -1
        if erc_cancelled:
            _log('ERC: キャンセルされました')
        erc_data = None
        if not erc_cancelled and os.path.exists(erc_out):
            with open(erc_out, encoding='utf-8') as f:
                erc_data = json.load(f)
            violations = [v for v in _erc_violations(erc_data) if not v.get('excluded')]
            errors   = [v for v in violations if v.get('severity') == 'error']
            warnings = [v for v in violations if v.get('severity') == 'warning']
            _log(f'ERC: {len(errors)} error(s), {len(warnings)} warning(s)')
            ignored = erc_data.get('ignored_checks') or []
            if ignored:
                _log(f'ERC: {len(ignored)} checks ignored: '
                     + ', '.join(c.get('key', '') for c in ignored))
            for v in errors:
                _log(f'  error: {v.get("description", "")}')
            for v in warnings:
                _log(f'  warning: {v.get("description", "")}')
        elif not erc_cancelled:
            _log('ERC: failed to run')
            if erc_proc.stderr:
                _log(erc_proc.stderr.strip())

        # AI レビュー形式への変換
        _log('generating review...')
        components, pins_by_comp, nets, power_nets = parse_netlist(root_elem)
        anomalies = detect_anomalies(components, pins_by_comp, power_nets)
        design_notes = read_design_notes(
            root_elem, sch_file,
            on_error=lambda path, exc: _log(f'warning: 回路図を読めません: {path} ({exc})'))
        review_text = format_review(
            sch_file, components, pins_by_comp, nets, power_nets, anomalies,
            erc_data=erc_data, erc_cancelled=erc_cancelled,
            design_notes=design_notes)
        with open(review_out, 'w', encoding='utf-8') as f:
            f.write(review_text)
        n_crit = len([a for a in anomalies if a[0] == 'CRITICAL'])
        n_warn = len([a for a in anomalies if a[0] == 'WARNING'])
        _log(f'review:   {review_out}')
        _log(f'anomalies: {n_crit} critical, {n_warn} warning')
        # 見出しの付け忘れや、回路図を保存せずにエクスポートした場合に気づけるよう
        # 件数を出す（0 件なら回路図側の見出しを疑う）
        _log(f'design notes: {len(design_notes)} block(s)')
        _log('done')

    finally:
        if _log_file:
            _log_file.close()
        if not keep_intermediates:
            for path in [net_out, erc_out, net_out + '.log']:
                try:
                    os.remove(path)
                except OSError:
                    pass


if __name__ == '__main__':
    main()
