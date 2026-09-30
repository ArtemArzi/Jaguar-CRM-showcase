# CRM «ЯГУАР»

CRM для спортивного клуба: desktop-админка, мобильная PWA для тренера, ученика и родителя, планшетный киоск чекина.

**[Кейсы, интерфейсы и код](https://artemarzi.github.io/portfolio/projects/)** · **[Все скриншоты CRM](https://artemarzi.github.io/portfolio/jaguar-crm/)**

## Задача и мой вклад

Я занимался операционной работой клуба: общался с тренерами, работал с обращениями, оплатами и расчётами. CRM создавалась, чтобы связать клиентов, абонементы, посещения, расписание и оплаты в одной системе.

Самостоятельно прорабатывал пользовательские сценарии, backend и интерфейсы, проверки и поэтапное внедрение. Основные AI-инструменты разработки: **Codex и Claude Code**. CRM — рабочий продукт с возможностью дальнейшего развития. В текущей версии остаются крупные модули, которые можно рефакторить. Демонстрационные данные не являются бизнес-результатами.

В CRM реализована рабочая интеграция с банком **«Точка»**. На демонстрационном стенде реальные платежи отключены.

## Что находится в репозитории

Это отдельный срез исходников для портфолио, без истории рабочего репозитория, рабочих данных и внутренних инструкций эксплуатации. Основной код приложения, миграции, интерфейсы, статические ресурсы и тесты сохранены.

| Папка | Содержание |
| --- | --- |
| `apps/` | Django-модули: клубы и роли, ученики, тренеры, занятия, оплаты, уведомления, документы |
| `config/` | Настройки Django, URL-маршруты и API |
| `frontend/src/` | React PWA: тренер, ученик, родитель, вход и киоск |
| `templates/`, `static/` | Админка на HTMX / Alpine / Tailwind и её ресурсы |
| `tests/`, `apps/*/tests/` | Backend-тесты и проверки пользовательских сценариев |
| `frontend/e2e/` | Playwright-сценарии |

Подробнее: [устройство и границы](docs/architecture.md), [проверки и состав копии](docs/showcase-scope.md).

## Сценарии пользователей

| Роль | Рабочая задача | Где посмотреть |
| --- | --- | --- |
| Администратор | Ученики, абонементы, расписание и оплаты | [Desktop-экраны](https://artemarzi.github.io/portfolio/jaguar-crm/) и `templates/` |
| Тренер | Свои ученики, занятия и рабочий день | `frontend/src/`, [мобильная PWA](https://artemarzi.github.io/portfolio/projects/#crm) |
| Ученик / родитель | Расписание, абонемент и информация о занятиях | React-интерфейсы и их API |
| Киоск | Отметка посещения на отдельном устройстве | `frontend/e2e/real-stack-kiosk.spec.ts` |
| Платёжный сценарий | Ссылка банка и обработка результата | `apps/billing/payment_providers/tochka.py` |

## Решения, которые имеет смысл оценить

- **Права и границы клубов.** Данные связаны с клубом, доступ проверяется backend. Матрица негативных сценариев: [tenant-negative](frontend/e2e/real-stack-tenant-negative-matrix.spec.ts).
- **Разные правила для разных действий.** Расписание, посещение, абонемент и расчёт выплаты не сведены к одному статусу. Проверки: [check-in lifecycle](frontend/e2e/real-stack-checkin-lifecycle.spec.ts), [self-booking](frontend/e2e/real-stack-student-parent-self-booking.spec.ts).
- **Внешний банк.** Ссылка и webhook требуют согласования состояния с провайдером. Примеры проверок: [webhooks](apps/billing/tests/test_tochka_webhooks.py), [reconciliation](apps/billing/tests/test_provider_reconciliation.py).
- **Несколько интерфейсов одного продукта.** Desktop, роли PWA и киоск используют общий backend; проверку прав нельзя заменять скрытием кнопки.

Наличие этих тестов — материал для чтения кода. Их полный повторный запуск 30.09.2026 не выполнялся. Исторические выполненные проверки публичной копии сохранены в [showcase-scope](docs/showcase-scope.md).

## Как я вёл работу

Разбирался в ежедневном процессе, фиксировал сценарии и правила, собирал решение с Codex и Claude Code, проверял интерфейсы и ошибки, внедрял поэтапно. Самостоятельная работа включает продуктовые решения, backend, frontend, интеграции и документацию; это не заявление о senior-уровне или завершении всех модулей.

[Сценарии и карта материалов](docs/scenarios.md) · [Подход к работе](https://github.com/ArtemArzi/portfolio/blob/main/docs/working-method.md)

## Локальный запуск

Понадобятся Python 3.12, Node.js 22+, npm, PostgreSQL 16 и Redis 7. Docker Compose можно использовать только для локальных БД и кэша:

```bash
docker compose -f compose.local.yml up -d
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements/local.txt
cp .env.example .env
```

В `.env` замените `SECRET_KEY` случайной строкой. Создайте отдельный локальный JWT-ключ; файл исключён из Git:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt-key.pem
chmod 600 jwt-key.pem
python manage.py migrate
python manage.py seed_test_data --new-club --name="Демо-клуб" --city="Уфа"
python manage.py runserver 127.0.0.1:8001
```

В другом терминале:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Откройте `http://127.0.0.1:5173/app`. Админка находится по `/dashboard/login/`, киоск по `/kiosk/`. Seed создаёт роли с email вида `owner.club1@example.com`, `trainer.club1@example.com`, `student.club1@example.com`, `parent.club1@example.com` (число зависит от ID клуба). Задайте собственный локальный пароль командой `python manage.py changepassword club1_owner` и аналогично для нужных ролей. Seed предназначен только для отдельной учебной базы.

Для просмотра экранов worker не нужен. Для фоновых задач в своём тестовом окружении: `python manage.py qcluster`. Настройки внешних платежей, Telegram и push-уведомлений по умолчанию не заполнены.

## Проверки

```bash
python manage.py check
pytest apps/students/tests apps/clubs/tests tests/test_management_support_commands.py
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
```

Эти команды не являются заявлением о прохождении полного production-релизного набора. Выполненные проверки именно этой копии перечислены в [showcase-scope.md](docs/showcase-scope.md).

Автор: Артемий Бражников.
