# Проверить статус сервиса
sudo systemctl restart openai_usage_bot.service

# Посмотреть логи сервиса (можно использовать -f для просмотра в реальном времени)
sudo journalctl -u openai_usage_bot.service -f