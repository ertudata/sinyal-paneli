#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Vadeli İşlemler (Futures) Temassız Sanal İşlem (Paper Trading) Botu
-------------------------------------------------------------------------
Geliştirici: Uzman Algo-Trading ve Python Yazılım Geliştiricisi
Açıklama: Bilgisayarın RAM belleğinde tamamen şifresiz, public verilerle çalışan,
          15 saniyelik bazda Mean Reversion (Ortalamaya Dönüş) stratejili simülatör.

Gereksinimler:
    pip install ccxt pandas rich
"""

import sys
import time
import datetime
import json
import os
import pandas as pd
import ccxt

# Rich Kütüphanesi Görsel Bileşenleri
from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.box import DOUBLE_EDGE, ROUNDED

# --- STRATEJİ VE KOMİSYON PARAMETRELERİ ---
SCAN_INTERVAL = 15            # Her tarama arasındaki süre (saniye)
PERCENT_THRESHOLD = 0.5       # Sinyal tetikleme yüzdesi (örn %0.5)
STOP_LOSS_PCT = 1.0           # Stop-Loss oranı (%1.0)
TAKE_PROFIT_PCT = 2.0         # Take-Profit oranı (%2.0)

# --- GELİŞMİŞ FİNANSAL SÜRTÜNME YAPISI ---
INITIAL_CAPITAL = 100000.0    # Başlangıç kasası ($100,000)
TOTAL_FRICTION_PCT = 0.15     # Toplam al-sat komisyon oranı
ENTRY_FEE_PCT = 0.075         # Alım komisyon oranı (%0.075)
EXIT_FEE_PCT = 0.075          # Satım komisyon oranı (%0.075)
POSITION_SIZE = 10000.0       # Sabit Pozisyon büyüklüğü ($10,000)

# --- PANEL ÇIKTI AYARLARI ---
SIGNALS_FILE = "signals.json"  # Panel tarafından okunan sinyal dosyası
MAX_SIGNALS  = 500             # Dosyada tutulacak maksimum sinyal sayısı

# --- SİSTEM DEĞİŞKENLERİ ---
current_balance = INITIAL_CAPITAL
total_fees_paid = 0.0
active_positions = {}
trade_history = []
balance_history = [INITIAL_CAPITAL]
system_logs = []
flash_status = {"message": "", "color": "white", "ticks": 0}

console = Console()

def add_system_log(message, type_str="info"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    if type_str == "success":
        color = "green"
    elif type_str == "warn":
        color = "yellow"
    elif type_str == "error":
        color = "red"
    elif type_str in ["signal_long", "signal_short"]:
        color = "magenta"
    else:
        color = "cyan"
    
    # Rich biçimlendirmeli log satırı
    log_line = f"[[dim]{timestamp}[/]] [[bold {color}]{type_str.upper()}[/]] {message}"
    system_logs.append(log_line)
    if len(system_logs) > 8:
        system_logs.pop(0)

# ── PANEL JSON YAZICI ────────────────────────────────────────────────────────
def save_signal_to_json(record: dict):
    """
    Bir sinyal kaydını (açılış veya kapanış) signals.json dosyasına ekler.
    Dosya panelin fetch ettiği tek veri kaynağıdır.
    record formatı:
        id, symbol, direction, status (OPEN|TP|SL),
        entry_price, exit_price (None ise null),
        sl, tp, pnl (None ise null),
        change_pct, timestamp
    """
    existing = []
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = []

    # Aynı id varsa güncelle (OPEN → TP/SL kapanışı), yoksa başa ekle
    updated = False
    for i, s in enumerate(existing):
        if s.get("id") == record["id"]:
            existing[i] = record
            updated = True
            break
    if not updated:
        existing.insert(0, record)

    # MAX_SIGNALS sınırına kırp
    existing = existing[:MAX_SIGNALS]

    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except IOError as e:
        add_system_log(f"signals.json yazma hatası: {e}", "error")


def generate_ascii_chart(history, height=5, width=44):
    """
    Kasa bakiye gelişimini terminal içerisine sığacak şekilde
    renkli metin bazlı bir çizgi grafik (sparkline) olarak çizer.
    """
    if len(history) < 2:
        # Başlangıçta boş grafik yerine düz çizgi simüle et
        history = [INITIAL_CAPITAL] * 5
        
    points = [float(v) for v in history][-width:]
    if len(points) < width:
        # Sol tarafı doldur
        points = [points[0]] * (width - len(points)) + points
        
    min_val = min(points)
    max_val = max(points)
    val_range = max_val - min_val
    if val_range == 0:
        val_range = 1.0
        
    grid = [[" " for _ in range(width)] for _ in range(height)]
    for i, val in enumerate(points):
        y = int(((val - min_val) / val_range) * (height - 1))
        y = max(0, min(height - 1, y))
        grid[height - 1 - y][i] = "█"
        
    lines = []
    for r in range(height):
        row_str = ""
        for char in grid[r]:
            if char == "█":
                row_str += "[bold cyan]█[/]"
            else:
                row_str += " "
        lines.append(row_str)
        
    chart_body = "\n".join(lines)
    return (
        f"[dim]---------------------------------------------[/]\n"
        f"{chart_body}\n"
        f"[dim]---------------------------------------------[/]\n"
        f"📈 [bold green]En Düşük: ${min_val:.2f}[/] | [bold red]En Yüksek: ${max_val:.2f}[/] | [bold yellow]Güncel: ${points[-1]:.2f}[/]"
    )

def make_layout() -> Layout:
    """
    Terminal ekranını bölen ve şık widget'ları konumlandıran Rich Layout nesnesi oluşturur.
    """
    layout = Layout()
    layout.split(
        Layout(name="header", size=4),
        Layout(name="body", size=18),
        Layout(name="footer", size=11)
    )
    layout["body"].split_row(
        Layout(name="left_panel", ratio=1),
        Layout(name="right_panel", ratio=1)
    )
    return layout

def update_dashboard_layout(layout: Layout, next_countdown: int, active_symbols_count: int):
    """
    Rich dashboard verilerini dinamik olarak günceller ve her saniye panelleri doldurur.
    """
    global current_balance, total_fees_paid, flash_status
    
    # 1. HEADER PANEL
    tp_count = sum(1 for t in trade_history if t['reason'] == 'TP')
    sl_count = sum(1 for t in trade_history if t['reason'] == 'SL')
    total_trades = len(trade_history)
    win_rate = (tp_count / total_trades * 100) if total_trades > 0 else 0.0
    
    header_text = Text()
    header_text.append("📈 ALGO-TRADING FUTURES PAPER BOTU  ", style="bold yellow")
    header_text.append(f"|  ZAMAN: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC  ", style="dim text")
    header_text.append(f"|  SONRAKİ TARAMA: {next_countdown}sn", style="bold cyan")
    
    layout["header"].update(
        Panel(
            header_text, 
            style="bold white on #0a0d16", 
            box=ROUNDED,
            title="[bold green]SİSTEM DURUMU: AKTİF (MEAN REVERSION)[/]"
        )
    )
    
    # 2. LEFT PANEL: KASA DETAYI & GRAFİK
    # Flashing Effect kontrolü
    balance_style = "bold green" if current_balance >= INITIAL_CAPITAL else "bold red"
    
    balance_text = Text()
    balance_text.append(f"💰 Sanal Bakiye: ", style="bold white")
    balance_text.append(f"${current_balance:.2f}\n", style=balance_style)
    balance_text.append(f"⚙️ Başlangıç Kasası: ${INITIAL_CAPITAL:.2f} | 🛡️ Toplam Komisyon Ödenen: ", style="dim text")
    balance_text.append(f"${total_fees_paid:.2f}\n\n", style="bold rose")
    
    if flash_status["ticks"] > 0:
        flash_status["ticks"] -= 1
        balance_text.append(f"{flash_status['message']}\n", style=flash_status["color"])
    else:
        balance_text.append("\n", style="dim text")
        
    chart_str = generate_ascii_chart(balance_history)
    left_panel_content = "\n".join([balance_text.markup, chart_str])
    
    layout["left_panel"].update(
        Panel(
            left_panel_content, 
            title="📊 PORTFÖY DEĞİŞİMİ & GRAFİK", 
            box=ROUNDED,
            border_style="cyan"
        )
    )
    
    # 3. RIGHT PANEL: AKTİF POZİSYONLAR TABLOSU
    pos_table = Table(box=ROUNDED, expand=True)
    pos_table.add_column("Sembol", style="bold white")
    pos_table.add_column("Yön", style="bold text")
    pos_table.add_column("Giriş Fiyatı", style="dim text", justify="right")
    pos_table.add_column("Anlık Fiyat", style="bold yellow", justify="right")
    pos_table.add_column("SL / TP", style="dim text", justify="center")
    pos_table.add_column("Net PnL%", style="bold green", justify="right")
    
    for symbol, pos in active_positions.items():
        # Bu sembole dair anlık ticker veya son fiyata bakacağız
        # Simülasyonda tickerlar yoksa son fiyatı varsayalım
        curr_price = pos['entry'] # normalde dinamik güncellenecek
        direction = pos['dir']
        
        # PnL hesaplaması
        pnl_pct = 0.0
        # Diğer değişkenler
        dir_badge = f"[bold green]LONG[/]" if direction == 'LONG' else f"[bold red]SHORT[/]"
        sl_tp = f"[dim]{pos['sl']:.3f} / {pos['tp']:.3f}[/]"
        pnl_str = f"[bold green]+0.00%[/]"
        
        pos_table.add_row(
            symbol.replace("/USDT:USDT", ""),
            dir_badge,
            f"${pos['entry']:.3f}",
            f"${curr_price:.3f}",
            sl_tp,
            pnl_str
        )
        
    layout["right_panel"].update(
        Panel(
            pos_table, 
            title=f"💼 AKTİF VADELİ POZİSYONLAR ({len(active_positions)})", 
            box=ROUNDED,
            border_style="yellow"
        )
    )
    
    # 4. FOOTER PANEL: LOGLAR & DETAYLI İSTATİSTİKLER
    stats_line = f"• [dim]Toplam İşlem:[/][bold] {total_trades}[/] | [dim]Kâr Al (TP):[/][bold green] {tp_count}[/] | [dim]Stop-Loss (SL):[/][bold red] {sl_count}[/] | [dim]Başarı Oranı:[/][bold yellow] %{win_rate:.2f}[/]\n"
    stats_line += f"───────────────────────────────────────────────────────────────\n"
    logs_body = "\n".join(system_logs)
    footer_content = stats_line + logs_body
    
    layout["footer"].update(
        Panel(
            footer_content, 
            title="⚠️ SİSTEM GÜNLÜĞÜ (SYSTEM LOGS)", 
            box=ROUNDED,
            border_style="magenta"
        )
    )

def initialize_exchange():
    """
    Binance vadeli işlemler borsasını API keysiz public modda hazırlar.
    """
    add_system_log("Binance Futures API bağlantısı kuruluyor...", "info")
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future'  # Vadeli işlemler modunu aktif eder
        }
    })
    return exchange

def fetch_futures_tickers(exchange):
    """
    Tüm aktif vadeli işlem çiftlerinin (/USDT) anlık fiyat, bid, ask verilerini çeker.
    NoneType değerlerine karşı korumalıdır.
    """
    try:
        tickers = exchange.fetch_tickers()
        usdt_tickers = {}
        for symbol, ticker in tickers.items():
            if 'USDT' in symbol and (':' in symbol or '/' in symbol):
                last_price = ticker.get('last')
                if last_price is None or last_price == 0:
                    continue
                
                bid_price = ticker.get('bid') or last_price
                ask_price = ticker.get('ask') or last_price

                usdt_tickers[symbol] = {
                    'symbol': symbol,
                    'last': float(last_price),
                    'bid': float(bid_price),
                    'ask': float(ask_price),
                    'timestamp': ticker.get('timestamp', time.time() * 1000)
                }
        return usdt_tickers
    except Exception as e:
        add_system_log(f"Veri çekilirken hata oluştu: {e}", "error")
        return {}

def check_and_close_positions(current_tickers):
    """
    Hafızadaki aktif pozisyonları güncel fiyatlarla kontrol eder.
    SL veya TP seviyelerine ulaşılmışsa pozisyonu kapatır ve bakiye günceller.
    """
    global current_balance, total_fees_paid, flash_status
    symbols_to_close = []
    
    for symbol, pos in active_positions.items():
        if symbol not in current_tickers:
            continue
            
        current_data = current_tickers[symbol]
        curr_price = current_data['last']
        direction = pos['dir']
        entry_price = pos['entry']
        
        # Kar/Zarar Yüzdesi Hesapla
        if direction == 'LONG':
            pnl_pct = ((curr_price - entry_price) / entry_price) * 100
            if curr_price <= pos['sl']:
                symbols_to_close.append((symbol, 'SL', pos['sl'], -STOP_LOSS_PCT))
            elif curr_price >= pos['tp']:
                symbols_to_close.append((symbol, 'TP', pos['tp'], TAKE_PROFIT_PCT))
        else: # SHORT
            pnl_pct = ((entry_price - curr_price) / entry_price) * 100
            if curr_price >= pos['sl']:
                symbols_to_close.append((symbol, 'SL', pos['sl'], -STOP_LOSS_PCT))
            elif curr_price <= pos['tp']:
                symbols_to_close.append((symbol, 'TP', pos['tp'], TAKE_PROFIT_PCT))

    # Pozisyonları kaldır, finansal friction kes ve bakiyeyi güncelle
    for symbol, close_reason, close_price, final_pnl in symbols_to_close:
        pos = active_positions.pop(symbol)
        duration = time.time() - pos['time']
        
        # Gelişmiş Komisyon Hesabı
        # Alış Komisyonu ($1000 nominal miktar üzerinden %0.075) = $0.75
        entry_fee = POSITION_SIZE * (ENTRY_FEE_PCT / 100) # $0.75
        # Satış Komisyonu (Kapanış nominal miktarı üzerinden %0.075)
        exit_fee = POSITION_SIZE * (1 + final_pnl / 100) * (EXIT_FEE_PCT / 100)
        total_trade_fee = entry_fee + exit_fee
        
        # Gross (Kaba) Kazanç/Zarar
        gross_pnl_val = POSITION_SIZE * (final_pnl / 100)
        # Net Kazanç/Zarar (Komisyon çıktısı)
        net_pnl_val = gross_pnl_val - total_trade_fee
        
        # Bakiyeyi Güncelle
        current_balance += net_pnl_val
        total_fees_paid += total_trade_fee
        balance_history.append(current_balance)
        
        trade_record = {
            'symbol': symbol,
            'direction': pos['dir'],
            'entry_price': pos['entry'],
            'exit_price': close_price,
            'reason': close_reason,
            'pnl_pct': final_pnl,
            'net_pnl_val': net_pnl_val,
            'fee_paid': total_trade_fee,
            'duration_secs': round(duration, 1),
            'timestamp': time.time()
        }
        trade_history.append(trade_record)
        # Panel JSON güncelle — OPEN kaydını TP/SL ile güncelle
        save_signal_to_json({
            "id": pos.get("id", f"{symbol}_{int(pos['time'])}"),
            "symbol": symbol.replace("/USDT:USDT", ""),
            "direction": pos["dir"],
            "status": close_reason,
            "entry_price": pos["entry"],
            "exit_price": close_price,
            "sl": pos["sl"],
            "tp": pos["tp"],
            "pnl": round(net_pnl_val, 2),
            "change_pct": round(final_pnl, 3),
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
        })
        
        # Flash visual effect trigger
        if close_reason == "TP":
            flash_status = {
                "message": f"▲ [KASA GÜNCELLENDİ] +${net_pnl_val:+.2f} ({symbol.replace('/USDT:USDT', '')} Kâr Al Kapatıldı)",
                "color": "blink bold green",
                "ticks": 3
            }
            add_system_log(f"Kâr Al (TP) Tetiklendi: {symbol} | Yön: {pos['dir']} | PnL: %{final_pnl:+.2f} | Net: ${net_pnl_val:+.2f} (Ödenen Komisyon: ${total_trade_fee:.2f})", "success")
        else:
            flash_status = {
                "message": f"▼ [KASA GÜNCELLENDİ] {net_pnl_val:+.2f}$ ({symbol.replace('/USDT:USDT', '')} Stop-Loss Kapatıldı)",
                "color": "blink bold red",
                "ticks": 3
            }
            add_system_log(f"Stop-Loss (SL) Tetiklendi: {symbol} | Yön: {pos['dir']} | PnL: %{final_pnl:+.2f} | Net: ${net_pnl_val:+.2f} (Ödenen Komisyon: ${total_trade_fee:.2f})", "warn")

def print_performance_report():
    """
    Program CTRL+C ile kesildiğinde ekrana basılan detaylı Simülasyon Performans Raporu.
    """
    console.print("\n" + "="*70, style="dim yellow")
    console.print("        SİMÜLASYON PERFORMANS RAPORU / PORTFÖY ÖZETİ (RICH TERMINAL)       ", style="bold yellow")
    console.print("="*70 + "\n", style="dim yellow")
    
    total_trades = len(trade_history)
    
    if total_trades == 0:
        console.print("[bold red][RAPOR][/] Simülasyon boyunca tamamlanmış (kapatılmış) işlem bulunmamaktadır.", style="yellow")
    else:
        tp_count = sum(1 for t in trade_history if t['reason'] == 'TP')
        sl_count = sum(1 for t in trade_history if t['reason'] == 'SL')
        win_rate = (tp_count / total_trades) * 100
        
        # Toplam Net Kar Hesabı
        total_net_gains = current_balance - INITIAL_CAPITAL
        
        console.print(f"💰 [bold white]Başlangıç Kasası:[/] ${INITIAL_CAPITAL:.2f}")
        console.print(f"💰 [bold white]Son Bakiye:[/] ${current_balance:.2f}")
        console.print(f"📐 [bold white]Toplam Biriken Getiri:[/] [bold {'green' if total_net_gains >= 0 else 'red'}]${total_net_gains:+.2f} ({((current_balance - INITIAL_CAPITAL)/INITIAL_CAPITAL)*100:+.2f}%)[/]")
        console.print(f"🛡️ [bold white]Ödenen Toplam Komisyon:[/] ${total_fees_paid:.2f} (Sürtünme)")
        console.print(f"📦 [bold white]Toplam Kapanan Pozisyon:[/ Black] {total_trades}")
        console.print(f"✅ [bold green]Kâr Al (TP) ile Kapanan:[/] {tp_count}")
        console.print(f"❌ [bold red]Stop-Loss (SL) ile Kapanan:[/] {sl_count}")
        console.print(f"🎯 [bold yellow]Yapay Win Rate (Başarı Oranı):[/] %{win_rate:.2f}")
        
        # DataFrame listesi basımı
        df = pd.DataFrame(trade_history)
        console.print("\n" + "-"*70, style="dim text")
        console.print("[bold cyan]KAPATILAN İŞLEMLER DETAYLI LİSTESİ:[/]\n")
        
        table = Table(show_header=True, header_style="bold magenta", box=ROUNDED)
        table.add_column("Sembol")
        table.add_column("Yön")
        table.add_column("Giriş")
        table.add_column("Çıkış")
        table.add_column("Neden")
        table.add_column("Net Kazanç")
        table.add_column("Komisyon")
        table.add_column("Süre (sn)")
        
        for tr in trade_history:
            net_style = "bold green" if tr['net_pnl_val'] >= 0 else "bold red"
            table.add_row(
                tr['symbol'].replace("/USDT:USDT", ""),
                tr['direction'],
                f"${tr['entry_price']:.4f}",
                f"${tr['exit_price']:.4f}",
                tr['reason'],
                f"${tr['net_pnl_val']:+.2f}",
                f"${tr['fee_paid']:.2f}",
                f"{tr['duration_secs']}s"
            )
        console.print(table)
        
    console.print("-"*70, style="dim text")
    active_count = len(active_positions)
    console.print(f"💼 [bold white]Şu An Hâlâ Açık Olan Pozisyon Sayısı:[/] {active_count}")
    if active_count > 0:
        for sym, pos in active_positions.items():
            duration = time.time() - pos['time']
            console.print(f"  - {sym} | Yön: {pos['dir']} | Giriş: {pos['entry']:.4f} | SL: {pos['sl']:.4f} | TP: {pos['tp']:.4f} | {round(duration, 1)} saniyedir açık")
            
    console.print("="*70, style="dim yellow")
    console.print("[SİSTEM] Simülasyon başarıyla sonlandırıldı. Bol kazançlar!\n", style="bold green")

def main():
    global active_positions
    add_system_log("Mean Reversion paper trading botu başlatılıyor...", "info")
    
    try:
        exchange = initialize_exchange()
        
        add_system_log("T_0 başlangıç fiyat verileri Binance sunucusundan çekiliyor...", "info")
        t0_tickers = fetch_futures_tickers(exchange)
        if not t0_tickers:
            console.print("[bold red][HATA][/] T_0 başlangıç fiyatları Binance API sunucusundan alınamadı.", style="red")
            return
            
        add_system_log(f"T_0 başarıyla alındı. {len(t0_tickers)} USDT vadeli işlem sembolü izlenmektedir.", "success")
        
        dashboard_layout = make_layout()
        countdown = SCAN_INTERVAL
        
        # Live display context'i
        with Live(dashboard_layout, refresh_per_second=1, screen=True) as live:
            while True:
                # Her döngüde saniyelik gerisayım azaltma ve GUI güncelleme
                for s in range(SCAN_INTERVAL, 0, -1):
                    update_dashboard_layout(dashboard_layout, s, len(active_positions))
                    time.sleep(1)
                    
                # 1. SCAN DÖNGÜSÜ BAŞLADI
                update_dashboard_layout(dashboard_layout, 0, len(active_positions))
                
                # 2. T_1 güncel verileri borsa üzerinden çek
                t1_tickers = fetch_futures_tickers(exchange)
                if not t1_tickers:
                    add_system_log("T_1 verileri borsa bağlantı hatası sebebiyle çekilemedi, bir sonraki döngü bekleniyor.", "error")
                    continue
                    
                # 3. Yüzen aktif pozisyonları kapatma koşulları kontrol et (SL/TP)
                check_and_close_positions(t1_tickers)
                
                # 4. Sinyal taraması gerçekleştir
                activated_signals = 0
                for symbol, t1_data in t1_tickers.items():
                    if symbol not in t0_tickers or symbol in active_positions:
                        continue
                        
                    t0_p = t0_tickers[symbol]['last']
                    t1_last = t1_data['last']
                    
                    price_change_pct = ((t1_last - t0_p) / t0_p) * 100
                    
                    bid_price = t1_data['bid']
                    ask_price = t1_data['ask']
                    
                    # 4a. YÜKSELİŞ -> SHORT REVERSION SİNYALİ GİRİŞİ
                    if price_change_pct >= PERCENT_THRESHOLD:
                        entry_price = bid_price
                        sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
                        tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
                        
                        # Alış esnasında %0.075 komisyon bütçeden düşülmeli
                        # (Komisyon kasa bakiyesinden doğrudan dökülüp kalıcı olarak yansıtılır)
                        global current_balance, total_fees_paid
                        entry_fee = POSITION_SIZE * (ENTRY_FEE_PCT / 100) # $0.75
                        current_balance -= entry_fee
                        total_fees_paid += entry_fee
                        
                        sig_id = f"{symbol}_{int(time.time())}"
                        active_positions[symbol] = {
                            'dir': 'SHORT',
                            'entry': entry_price,
                            'sl': sl_price,
                            'tp': tp_price,
                            'time': time.time(),
                            'id': sig_id
                        }
                        activated_signals += 1
                        add_system_log(f"SHORT Sinyal Tetiklendi: {symbol.replace('/USDT:USDT', '')} | 15s Değişim: %{price_change_pct:+.2f} | Giriş: {entry_price} (Bid) | Komisyon Kesintisi: ${entry_fee:.2f}", "signal_short")
                        save_signal_to_json({
                            "id": sig_id,
                            "symbol": symbol.replace("/USDT:USDT", ""),
                            "direction": "SHORT",
                            "status": "OPEN",
                            "entry_price": entry_price,
                            "exit_price": None,
                            "sl": sl_price,
                            "tp": tp_price,
                            "pnl": None,
                            "change_pct": round(price_change_pct, 3),
                            "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
                        })
                    
                    # 4b. DÜŞÜŞ -> LONG REVERSION SİNYALİ GİRİŞİ
                    elif price_change_pct <= -PERCENT_THRESHOLD:
                        entry_price = ask_price
                        sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
                        tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
                        
                        # Alış esnasında %0.075 komisyon bütçeden düşülmeli
                        entry_fee = POSITION_SIZE * (ENTRY_FEE_PCT / 100) # $0.75
                        current_balance -= entry_fee
                        total_fees_paid += entry_fee
                        
                        sig_id = f"{symbol}_{int(time.time())}"
                        active_positions[symbol] = {
                            'dir': 'LONG',
                            'entry': entry_price,
                            'sl': sl_price,
                            'tp': tp_price,
                            'time': time.time(),
                            'id': sig_id
                        }
                        activated_signals += 1
                        add_system_log(f"LONG Sinyal Tetiklendi: {symbol.replace('/USDT:USDT', '')} | 15s Değişim: %{price_change_pct:+.2f} | Giriş: {entry_price} (Ask) | Komisyon Kesintisi: ${entry_fee:.2f}", "signal_long")
                        save_signal_to_json({
                            "id": sig_id,
                            "symbol": symbol.replace("/USDT:USDT", ""),
                            "direction": "LONG",
                            "status": "OPEN",
                            "entry_price": entry_price,
                            "exit_price": None,
                            "sl": sl_price,
                            "tp": tp_price,
                            "pnl": None,
                            "change_pct": round(price_change_pct, 3),
                            "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
                        })

                # Mevcut T_1 verisini bir sonraki döngü için başlangıç noktası T_0 yapıyoruz
                t0_tickers = t1_tickers
                
                # Bilgi satırı yaz
                status_msg = f"Tarama sonlandı. Aktif Pozisyon: {len(active_positions)} | Biriken Trade: {len(trade_history)}"
                if activated_signals > 0:
                    status_msg += f" | Bu taramada {activated_signals} yeni işlem."
                add_system_log(status_msg, "info")
                
    except KeyboardInterrupt:
        print_performance_report()
    except Exception as e:
        console.print(f"[bold red][SİSTMEDİ KRİTİK HATA][/] Program beklenmedik durum hatasından çöktü: {e}", style="red")
        if len(trade_history) > 0 or len(active_positions) > 0:
            print_performance_report()

if __name__ == '__main__':
    main()
