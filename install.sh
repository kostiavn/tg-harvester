#!/bin/bash
# Установка TG Harvester в LXC контейнер (Debian/Ubuntu)
# Запускать от root.

set -e

INSTALL_DIR="/opt/tg-harvester"

echo "==> [1/6] Обновляем систему и ставим Python"
apt update -y
apt install -y python3 python3-pip python3-venv git

echo "==> [2/6] Создаём директорию $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "==> [3/6] Копируем файлы (из текущей директории)"
cp $(dirname "$0")/harvester.py "$INSTALL_DIR/"
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    cp $(dirname "$0")/config.example.yaml "$INSTALL_DIR/config.yaml"
    echo "   создан config.yaml — ВАЖНО: заполни api_id и api_hash перед первым запуском"
fi

echo "==> [4/6] Ставим Python зависимости (venv)"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install telethon pyyaml

echo "==> [5/6] Устанавливаем systemd unit и timer"

cat > /etc/systemd/system/tg-harvester.service <<EOF
[Unit]
Description=TG Harvester — одноразовый забор сообщений
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/harvester.py harvest
Nice=10
EOF

cat > /etc/systemd/system/tg-harvester.timer <<EOF
[Unit]
Description=TG Harvester — расписание

[Timer]
# 4 раза в сутки: 00:00, 06:00, 12:00, 15:00 UTC
# (15:00 UTC = 18:00 МСК, забираем ДО вайпа в 16:00 МСК если он по UTC,
#  либо измени под свой часовой пояс)
OnCalendar=*-*-* 00,06,12,15:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Архивация раз в неделю в воскресенье в 23:00 — отправит tar.gz в Saved Messages
cat > /etc/systemd/system/tg-harvester-archive.service <<EOF
[Unit]
Description=TG Harvester — еженедельный архив
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/harvester.py archive
EOF

cat > /etc/systemd/system/tg-harvester-archive.timer <<EOF
[Unit]
Description=TG Harvester — расписание архивации

[Timer]
OnCalendar=Sun *-*-* 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Listener для команд в Saved Messages (опционально, демон)
cat > /etc/systemd/system/tg-harvester-listen.service <<EOF
[Unit]
Description=TG Harvester — listener (Saved Messages commands)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/harvester.py listen
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "==> [6/6] Готово. Что делать дальше:"
echo ""
echo "   1) Заполни config.yaml: nano $INSTALL_DIR/config.yaml"
echo "      (api_id, api_hash, чаты)"
echo ""
echo "   2) Первичная авторизация (один раз, интерактивно):"
echo "      cd $INSTALL_DIR && ./venv/bin/python harvester.py auth"
echo "      (введёт телефон → код из Telegram → опционально 2FA)"
echo ""
echo "   3) Тестовый запуск:"
echo "      ./venv/bin/python harvester.py harvest"
echo ""
echo "   4) Включить расписание:"
echo "      systemctl enable --now tg-harvester.timer"
echo "      systemctl enable --now tg-harvester-archive.timer"
echo ""
echo "   5) (Опционально) Listener для команд в Saved Messages:"
echo "      systemctl enable --now tg-harvester-listen.service"
echo ""
echo "   Проверка таймеров:    systemctl list-timers | grep tg-harvester"
echo "   Логи:                 journalctl -u tg-harvester.service -f"
echo "                         tail -f $INSTALL_DIR/logs/harvester.log"
