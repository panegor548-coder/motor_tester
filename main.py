"""
Motor Test Stand — настольное приложение (Windows .exe через PyInstaller)
============================================================================

Отличия от присланной тобой версии (исправленные баги):
  1. Обновление GUI и графика ИДЁТ ТОЛЬКО из главного потока — чтение Serial
     работает в фоновом потоке, но пишет строки в очередь (queue.Queue),
     а GUI разбирает очередь через self.after() каждые 50 мс. Раньше
     matplotlib/Tkinter дёргались напрямую из фонового потока — риск
     падения, особенно на macOS/Windows под PyInstaller.
  2. Список COM/USB-портов — выпадающий список с автообновлением,
     а не хардкод пути к устройству.
  3. График встроен в окно (FigureCanvasTkAgg), обновляется по ходу теста,
     а не открывается отдельным блокирующим окном plt.show() в конце.
  4. Протокол разбора подогнан под текущую прошивку ESP32 (JSON-строки),
     см. README в репозитории.

Зависимости: customtkinter, pyserial, matplotlib (см. requirements.txt)
"""

import json
import queue
import threading
import time
import csv
from datetime import datetime

import customtkinter as ctk
import serial
import serial.tools.list_ports
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

BAUD_RATE = 115200


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Стенд тестирования моторов")
        self.geometry("900x650")

        self.serial_port = None
        self.reader_thread = None
        self.stop_reader = threading.Event()
        self.rx_queue = queue.Queue()
        self.is_running = False

        self.data = {"t": [], "pwm": [], "throttle_pct": [], "rpm": [],
                     "voltage_raw": [], "current_raw": [], "thrust": []}

        self._build_ui()
        self._refresh_ports()
        self.after(50, self._poll_queue)

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=16, pady=(16, 8))

        self.port_combo = ctk.CTkComboBox(top, values=[], width=220)
        self.port_combo.pack(side="left", padx=(0, 8))

        ctk.CTkButton(top, text="Обновить порты", width=130,
                      command=self._refresh_ports).pack(side="left", padx=4)

        self.btn_connect = ctk.CTkButton(top, text="Подключить", width=120,
                                          command=self.connect)
        self.btn_connect.pack(side="left", padx=4)

        self.label_status = ctk.CTkLabel(self, text="Не подключено",
                                          font=ctk.CTkFont(size=14, weight="bold"))
        self.label_status.pack(pady=(4, 8))

        self.label_throttle = ctk.CTkLabel(self, text="Газ: 0%",
                                            font=ctk.CTkFont(size=30, weight="bold"))
        self.label_throttle.pack(pady=4)

        stats = ctk.CTkFrame(self)
        stats.pack(fill="x", padx=16, pady=8)
        self.stat_labels = {}
        for i, (key, title) in enumerate([
            ("rpm", "RPM"), ("voltage_raw", "Вольты (АЦП)"),
            ("current_raw", "Ампер (АЦП)"), ("thrust", "Тяга")
        ]):
            f = ctk.CTkFrame(stats)
            f.grid(row=0, column=i, padx=8, pady=8, sticky="nsew")
            stats.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=11)).pack(pady=(6, 0))
            val = ctk.CTkLabel(f, text="0", font=ctk.CTkFont(size=20, weight="bold"))
            val.pack(pady=(0, 6))
            self.stat_labels[key] = val

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=16, pady=8)
        self.btn_start = ctk.CTkButton(btns, text="НАЧАТЬ ИСПЫТАНИЯ", fg_color="green",
                                        hover_color="darkgreen", height=50,
                                        font=ctk.CTkFont(size=14, weight="bold"),
                                        command=self.start_test, state="disabled")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.btn_stop = ctk.CTkButton(btns, text="АВАРИЙНЫЙ СТОП", fg_color="red",
                                       hover_color="darkred", height=50,
                                       font=ctk.CTkFont(size=16, weight="bold"),
                                       command=self.emergency_stop, state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(8, 0))

        self.btn_csv = ctk.CTkButton(self, text="Скачать CSV", command=self.export_csv,
                                      state="disabled")
        self.btn_csv.pack(pady=(0, 8))

        # Встроенный график
        self.fig = Figure(figsize=(8, 5), dpi=100, facecolor="#1e1e1e")
        self.axs = self.fig.subplots(2, 2)
        for ax in self.axs.flat:
            ax.set_facecolor("#1e1e1e")
            ax.tick_params(colors="white", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("white")
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(0, 16))

    # ---------------- Порты и подключение ----------------
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo.configure(values=ports)
        if ports:
            self.port_combo.set(ports[0])

    def connect(self):
        port = self.port_combo.get()
        if not port:
            self.label_status.configure(text="Выбери порт", text_color="orange")
            return
        try:
            self.serial_port = serial.Serial(port, BAUD_RATE, timeout=0.2)
            time.sleep(2)  # ESP32 перезагружается при открытии порта
            self.stop_reader.clear()
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()
            self.label_status.configure(text=f"Подключено: {port}", text_color="lightgreen")
            self.btn_start.configure(state="normal")
            self.btn_connect.configure(state="disabled")
        except Exception as e:
            self.label_status.configure(text=f"Ошибка подключения: {e}", text_color="red")

    def _read_loop(self):
        # Работает в фоновом потоке — ТОЛЬКО читает и кладёт в очередь,
        # никакого обращения к GUI/matplotlib отсюда.
        while not self.stop_reader.is_set():
            try:
                line = self.serial_port.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    self.rx_queue.put(line)
            except Exception:
                break

    def _poll_queue(self):
        # Работает в главном потоке — безопасно обновляет GUI/график
        try:
            while True:
                line = self.rx_queue.get_nowait()
                self._handle_line(line)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    # ---------------- Логика теста ----------------
    def start_test(self):
        if self.is_running or not self.serial_port:
            return
        for k in self.data:
            self.data[k].clear()
        self.btn_csv.configure(state="disabled")
        self.serial_port.write(b"START\n")
        self.is_running = True
        self.label_status.configure(text="Идёт автоматический тест...", text_color="yellow")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def emergency_stop(self):
        if self.serial_port:
            self.serial_port.write(b"STOP\n")
        self.is_running = False
        self.label_status.configure(text="ТЕСТ ПРИНУДИТЕЛЬНО ПРЕРВАН", text_color="red")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _handle_line(self, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return

        if "status" in obj:
            status = obj["status"]
            reason = obj.get("reason", "")
            if status == "DONE":
                self.label_status.configure(text="Тест успешно завершён", text_color="lightgreen")
            elif status == "STOPPED":
                self.label_status.configure(text="Остановлено пользователем", text_color="orange")
            elif status == "SAFETY_CUTOFF":
                self.label_status.configure(text=f"Защита сработала: {reason}", text_color="red")
            self.is_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            if self.data["t"]:
                self.btn_csv.configure(state="normal")
            return

        for k in self.data:
            if k in obj:
                self.data[k].append(obj[k])

        self.label_throttle.configure(text=f"Газ: {obj.get('throttle_pct', 0)}%")
        for k, lbl in self.stat_labels.items():
            if k in obj:
                lbl.configure(text=f"{obj[k]:.0f}" if isinstance(obj[k], (int, float)) else str(obj[k]))

        self._update_chart()

    def _update_chart(self):
        x = self.data["throttle_pct"]
        plots = [
            (self.axs[0, 0], self.data["voltage_raw"], "Напряжение (АЦП)", "#ffb84f"),
            (self.axs[0, 1], self.data["current_raw"], "Ток (АЦП)", "#ff4d4f"),
            (self.axs[1, 0], self.data["thrust"], "Тяга", "#4fff8f"),
            (self.axs[1, 1], self.data["rpm"], "Обороты (RPM)", "#4f9dff"),
        ]
        for ax, y, title, color in plots:
            ax.clear()
            ax.plot(x, y, color=color, marker="o", markersize=3)
            ax.set_title(title, color="white", fontsize=10)
            ax.set_facecolor("#1e1e1e")
            ax.tick_params(colors="white", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("white")
        self.canvas.draw_idle()

    def export_csv(self):
        if not self.data["t"]:
            return
        filename = f"motor_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        keys = list(self.data.keys())
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(keys)
            for i in range(len(self.data["t"])):
                writer.writerow([self.data[k][i] for k in keys])
        self.label_status.configure(text=f"Сохранено: {filename}", text_color="lightgreen")


if __name__ == "__main__":
    app = App()
    app.mainloop()
