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


def _run_cmd_with_progress(cmd, title='処理中...'):
    """コマンドを実行しながら MessageBoxW で進捗を表示する。
    Windows 以外は通常の subprocess.run と同等。
    """
    if sys.platform != 'win32':
        return subprocess.run(cmd, capture_output=True, text=True)
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
    MB_TOPMOST         = 0x00040000
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
                                    MB_ICONINFORMATION | MB_TOPMOST | MB_OKCANCEL)
        if hook_h[0]:   # フックが発火しなかった場合のフォールバック解除
            user32.UnhookWindowsHookEx(hook_h[0])
        if retval == IDCANCEL:
            cancel_event.set()

    mb_thread = threading.Thread(target=show_mb, daemon=True)
    mb_thread.start()
    kernel32.Sleep(300)   # MessageBoxW が描画されるまで待つ (ctypes call = GIL 解放)

    # ERC を非同期で開始し、ポーリングループで経過秒数を更新する
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

def parse_netlist(root_elem):
    """KiCad XML ネットリストを解析して (components, pins_by_comp, nets, power_nets) を返す。

    components:   ref -> {value, libpart, sheet}
    pins_by_comp: ref -> {pin -> {net, pintype, pinfunction}}
    nets:         net_name -> [{ref, pin, pintype, pinfunction}]
    power_nets:   power_out ノードを持つネット名の集合
    """
    components = {}
    for comp in root_elem.findall('components/comp'):
        ref = comp.get('ref', '')
        value = comp.findtext('value') or ''
        libsrc = comp.find('libsource')
        libpart = libsrc.get('part', '') if libsrc is not None else ''
        sheetpath = comp.find('sheetpath')
        sheet = sheetpath.get('names', '/') if sheetpath is not None else '/'
        components[ref] = {'value': value, 'libpart': libpart, 'sheet': sheet}

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


# ---------- 異常検出 ----------

def _is_positive_supply(net_name):
    n = net_name.upper().lstrip('/')
    return n.startswith('+') or any(
        n.startswith(p) for p in ('VCC', 'VDD', 'AVCC', 'AVDD'))


def _is_gnd(net_name):
    n = net_name.upper().lstrip('/')
    return n in ('GND', 'AGND', 'DGND', 'PGND') or n.startswith('GND')


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

        # パッシブ部品の両端が電源ネット
        if len(pins) == 2 and ref[:1].upper() in ('R', 'C', 'L', 'D', 'F'):
            pin_vals = list(pins.values())
            if all(p['net'] in power_nets for p in pin_vals):
                nets_str = ' / '.join(p['net'] for p in pin_vals)
                val = components.get(ref, {}).get('value', '')
                anomalies.append(('WARNING',
                    f'{ref}({val}) 両端が電源ネット [{nets_str}]'))

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


def format_review(source_file, components, pins_by_comp, nets, power_nets, anomalies,
                  erc_data=None, erc_cancelled=False):
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
        '  ANOMALIES   — ネットリスト解析による疑わしい接続の自動検出（人間のレビューが必要）',
        '  ERC RESULTS — KiCad の Electrical Rules Check（電気的規則検査）結果 JSON',
        '  COMPONENTS  — コンポーネント一覧とピン接続（電源ピンに ✓=正常 / !!=異常 のマーカー付き）',
        '  SIGNAL NETS — 信号ネット一覧（電源ネットを除く）',
        '',
        'ピンタイプ略称: pwr=電源, in=入力, out=出力, bi=双方向, tri=3ステート, pas=パッシブ',
        '',
    ]

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
        violations = erc_data.get('violations', [])
        n_err  = sum(1 for v in violations if v.get('severity') == 'error')
        n_warn = sum(1 for v in violations if v.get('severity') == 'warning')
        lines.append(f'== ERC RESULTS == ({n_err} error(s), {n_warn} warning(s))')
        lines.append('```json')
        lines.append(json.dumps(erc_data, ensure_ascii=False, indent=2))
        lines.append('```')
        lines.append('')

    # コンポーネント一覧（シート別）
    lines.append('== COMPONENTS ==')
    sheets = {}
    for ref in components:
        sheets.setdefault(components[ref]['sheet'], []).append(ref)

    for sheet in sorted(sheets):
        lines.append(f'\n[{sheet}]')
        for ref in sorted(sheets[sheet], key=_ref_sort_key):
            if ref.startswith('#'):  # 電源シンボル (#PWR, #FLG 等) は省略
                continue
            comp = components[ref]
            lines.append(f'  {ref}  {comp["value"]}  ({comp["libpart"]})')
            pins = pins_by_comp.get(ref, {})

            def pin_num_key(item):
                pin = item[0]
                try:
                    return (0, int(pin), '')
                except ValueError:
                    # 非数値ピンは必ず数値ピンの後ろに来るようにする
                    return (1, 0, pin)

            pwr_pins = sorted(
                [(p, i) for p, i in pins.items()
                 if i.get('pintype') in ('power_in', 'power_out')],
                key=pin_num_key)
            sig_pins = sorted(
                [(p, i) for p, i in pins.items()
                 if i.get('pintype') not in ('power_in', 'power_out')],
                key=pin_num_key)

            if pwr_pins:
                parts = []
                for pn, info in pwr_pins:
                    marker = pin_flag(info) if pin_flag(info) else ' ✓'
                    pf = info.get('pinfunction', '')
                    parts.append(f'pin{pn}({pf})→{info["net"]}{marker}')
                lines.append(f'    PWR: {" ".join(parts)}')

            for pn, info in sig_pins:
                flag = pin_flag(info)
                pf = info.get('pinfunction', '')
                pt = _pt_short(info.get('pintype', ''))
                lines.append(f'    pin{pn}({pf},{pt})={info["net"]}{flag}')

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
            violations = erc_data.get('violations', [])
            errors   = [v for v in violations if v.get('severity') == 'error']
            warnings = [v for v in violations if v.get('severity') == 'warning']
            _log(f'ERC: {len(errors)} error(s), {len(warnings)} warning(s)')
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
        review_text = format_review(
            sch_file, components, pins_by_comp, nets, power_nets, anomalies,
            erc_data=erc_data, erc_cancelled=erc_cancelled)
        with open(review_out, 'w', encoding='utf-8') as f:
            f.write(review_text)
        n_crit = len([a for a in anomalies if a[0] == 'CRITICAL'])
        n_warn = len([a for a in anomalies if a[0] == 'WARNING'])
        _log(f'review:   {review_out}')
        _log(f'anomalies: {n_crit} critical, {n_warn} warning')
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
