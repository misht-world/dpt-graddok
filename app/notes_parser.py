"""
Модуль для парсинга PDF документов с замечаниями (копия из dpt-notes).
Извлекает метаданные и разбивает текст на замечания по абзацам.
Поддерживает OCR для сканированных документов.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional
import pdfplumber
from datetime import datetime

# Copied from dpt-notes (backend/pdf_parser.py); dpt-notes itself is not touched.
# OCR is not used in ГРАДДОК: on the Pi only the text layer of the PDF is read.
from .notes_orgs import normalize_organization, ORGANIZATIONS  # noqa: F401

OCR_AVAILABLE = False


# Слова, которые стоят перед инициалами, но фамилией не являются
NOT_SURNAMES = {
    "комитет", "комитета", "комитетом", "председателя", "председатель",
    "администрация", "администрации", "управление", "управления",
    "отдел", "отдела", "центр", "центра", "директор", "директора",
    "начальник", "начальника", "заместитель", "заместителя",
}


class PDFParser:
    """Парсер PDF документов с замечаниями"""

    def __init__(self, pdf_path: str, use_ocr: bool = False):
        """
        Args:
            pdf_path: Путь к PDF файлу
            use_ocr: Использовать OCR для извлечения текста (автоопределение сканов)
        """
        self.pdf_path = pdf_path
        self.filename = os.path.basename(pdf_path)
        self.full_text = ""
        self.metadata = {}
        self.remarks = []
        self.use_ocr = use_ocr

    def extract_text(self) -> str:
        """
        Извлечение текста из PDF.
        Автоматически использует OCR для сканированных документов.
        """
        try:
            # Если включен OCR и модуль доступен - используем GOT-OCR 2.0
            if self.use_ocr and OCR_AVAILABLE:
                print(f"Режим OCR активирован для {self.filename}")
                # Используем GOT-OCR 2.0 для отличного распознавания кириллицы
                # force_ocr=True - принудительно используем OCR, игнорируя проверку текстового слоя
                self.full_text = extract_text_from_pdf(self.pdf_path, force_ocr=True, engine='got')
                return self.full_text

            # Стандартное извлечение текста
            with pdfplumber.open(self.pdf_path) as pdf:
                text_parts = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)

                self.full_text = "\n".join(text_parts)

                # Если текста очень мало и OCR доступен - пробуем GOT-OCR
                if len(self.full_text.strip()) < 100 and OCR_AVAILABLE:
                    print(f"Мало текста в {self.filename}, пробуем GOT-OCR...")
                    ocr_text = extract_text_from_pdf(self.pdf_path, engine='got')
                    if len(ocr_text) > len(self.full_text):
                        print(f"GOT-OCR дал лучший результат: {len(ocr_text)} vs {len(self.full_text)} символов")
                        self.full_text = ocr_text

                return self.full_text
        except Exception as e:
            print(f"Ошибка при извлечении текста из {self.filename}: {e}")
            return ""

    def extract_metadata(self) -> Dict[str, str]:
        """Извлечение метаданных документа по регулярным выражениям"""
        if not self.full_text:
            self.extract_text()

        metadata = {
            "filename": self.filename,
            "project_name": None,
            "organization": None,
            "document_number": None,
            "document_date": None,
            "signed_by": None,
        }

        # Исполнители из нижней части последней страницы
        performers = self._extract_performers()
        if performers:
            metadata["signed_by"] = performers

        # ПРИОРИТЕТ: Извлекаем номер и дату из верхнего левого угла (шапка документа)
        header_data = self._extract_number_date_from_header()
        if header_data['number']:
            metadata["document_number"] = header_data['number']
        if header_data['date']:
            metadata["document_date"] = header_data['date']

        # Если не нашли номер в шапке, ищем по всему тексту (запасной вариант)
        # ВАЖНО: Исключаем "На №" (входящий номер)
        if not metadata["document_number"]:
            number_patterns = [
                r"№\s*(\d{2}-\d{2}-\d{5}/\d{2}-\d-\d)",  # №01-19-14759/25-0-1
                r"№\s*(\d{2}-\d{4}/\d{2}-\d-\d)",        # № 02-6624/25-1-0
                r"№\s*([\d\-/]+)",                        # Общий паттерн (экранирован дефис)
            ]

            for pattern in number_patterns:
                matches = re.finditer(pattern, self.full_text)
                for match in matches:
                    # Проверяем контекст перед номером
                    match_start = match.start()
                    prefix = self.full_text[max(0, match_start-20):match_start]

                    # Исключаем если перед номером есть "На " (входящий номер)
                    if 'На ' not in prefix and 'на ' not in prefix:
                        metadata["document_number"] = match.group(1).strip()
                        break

                if metadata["document_number"]:
                    break

        # Если не нашли дату в шапке, ищем по всему тексту (запасной вариант)
        if not metadata["document_date"]:
            date_pattern = r"от\s+(\d{2}\.\d{2}\.\d{4})"
            date_match = re.search(date_pattern, self.full_text)
            if date_match:
                metadata["document_date"] = date_match.group(1).strip()

        # Организация из шапки документа, приведённая к сокращению (КРТИ, КПООС, ...)
        metadata["organization"] = self._extract_organization()

        # Поиск названия проекта (обычно в кавычках или после определенных ключевых слов)
        # Примеры: «Киевское шоссе от Магистрали № 1 до Красносельского шоссе»
        project_patterns = [
            r'[«"]([^»"]+шоссе[^»"]*)[»"]',
            r'линейного объекта[:\s]+[«"]([^»"]+)[»"]',
            r'размещения[:\s]+[«"]([^»"]+)[»"]',
        ]

        for pattern in project_patterns:
            match = re.search(pattern, self.full_text, re.IGNORECASE)
            if match:
                metadata["project_name"] = match.group(1).strip()
                break

        self.metadata = metadata
        return metadata

    def _extract_organization(self) -> Optional[str]:
        """
        Определяет организацию-отправителя и приводит её к сокращению.

        Название в шапке часто разорвано переносами строк
        ("КОМИТЕТ ПО\nГРАДОСТРОИТЕЛЬСТВУ И АРХИТЕКТУРЕ"), поэтому берём
        окно текста после ключевого слова и отдаём его в справочник.
        """
        if not self.full_text:
            return None

        # Шапка — начало документа; если там не нашлось, смотрим весь текст
        regions = [self.full_text[:1500], self.full_text]
        keywords = ("комитет", "администрац", "центр")

        for region in regions:
            lowered = region.lower().replace("ё", "е")
            positions = [lowered.find(word) for word in keywords]
            positions = [pos for pos in positions if pos >= 0]
            if not positions:
                continue

            start = min(positions)
            window = region[start:start + 160]
            window = re.sub(r"\s+", " ", window).strip()

            normalized = normalize_organization(window)
            if normalized in ORGANIZATIONS or (normalized or "").startswith("РА"):
                return normalized

        # Не опознали: возвращаем первую строку с ключевым словом как есть
        for line in self.full_text[:1500].split("\n"):
            cleaned = re.sub(r"\s+", " ", line).strip()
            if cleaned and any(word in cleaned.lower() for word in ("омитет", "дминистрац")):
                return cleaned[:120]

        return None

    def _extract_performers(self) -> Optional[str]:
        """
        Извлекает исполнителей письма — блок в самом низу последней страницы
        ("Шабан М.С. 417-59-27"), а не того, кто подписал документ.

        Отличие простое: исполнители пишутся как "Фамилия И.О.", подписант —
        как "И.О. Фамилия". Ищем только первый вариант, поэтому подписант
        в результат не попадает.

        Возвращает строку вида "Шабан М.С. (417-59-27), Балуев И.С. (417-40-75)".
        """
        try:
            text = self._bottom_text()
            if not text:
                return None

            # Если есть пометка "исп." — начинаем от неё, там точно исполнители
            marker = re.search(r"(?:исп(?:олнитель|\.)|тел(?:\.|ефон))", text, re.IGNORECASE)
            if marker:
                text = text[marker.start():]

            text = re.sub(r"[ \t]+", " ", text)

            # Фамилия И.О. — допускаем двойные фамилии и пробел между инициалами.
            # Отрицательный просмотр вперёд отсекает подписанта: у него после
            # инициалов идёт фамилия ("Комитета И.А. Серебрицкий").
            name_pattern = re.compile(
                r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ])\.\s?([А-ЯЁ])\.(?!\s*[А-ЯЁ][а-яё])"
            )
            # Городской номер 417-59-27, при необходимости с кодом: +7(812)417-59-27
            phone_pattern = re.compile(
                r"(?:[+]?[78][\s(-]*\d{3}\)?[\s-]?)?\d{3}[\s-]?\d{2}[\s-]?\d{2}"
            )

            performers = []
            seen = set()

            for match in name_pattern.finditer(text):
                surname, first, second = match.groups()
                if surname.lower() in NOT_SURNAMES:
                    continue
                name = f"{surname} {first}.{second}."

                # Хвост после имени: там обычно стоит телефон исполнителя
                tail = text[match.end():match.end() + 40]
                phone_match = phone_pattern.search(tail)
                phone = phone_match.group(0).strip() if phone_match else None

                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)

                performers.append(f"{name} ({phone})" if phone else name)

            if performers:
                return ", ".join(performers)

            return None

        except Exception as e:
            print(f"Ошибка при извлечении исполнителей: {e}")
            return None

    def _bottom_text(self) -> Optional[str]:
        """Текст нижней части последней страницы, где печатают исполнителей."""
        # Если текст получен через OCR — координат нет, берём хвост текста
        if self.full_text and len(self.full_text) > 100:
            return self.full_text[int(len(self.full_text) * 0.6):]

        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                last_page = pdf.pages[-1]
                bbox = (
                    0,                              # левый край
                    last_page.height * 0.7,         # нижняя треть страницы
                    last_page.width * 0.6,          # левая часть
                    last_page.height,
                )
                return last_page.crop(bbox).extract_text()
        except Exception as e:
            print(f"Не удалось прочитать низ страницы: {e}")
            return None

    def _extract_number_date_from_header(self) -> Dict[str, Optional[str]]:
        """
        Извлечение номера и даты исходящего документа из верхнего ЛЕВОГО угла первой страницы.

        Стратегия:
        1. Сначала ищем в левом верхнем углу (там обычно шапка с номером и датой)
        2. Если не найдено и доступен OCR - используем полный OCR текст
        3. Исключаем "На №" (это входящий номер письма, а не исходящий)

        Возвращает словарь с ключами 'number' и 'date'
        """
        result = {'number': None, 'date': None}

        try:
            # Если уже есть OCR-текст - используем его (он заполняется в extract_text)
            text_to_search = self.full_text if self.full_text else ""

            # Если OCR не использовался, пробуем стандартное извлечение из левого угла
            if not text_to_search:
                with pdfplumber.open(self.pdf_path) as pdf:
                    first_page = pdf.pages[0]
                    page_height = first_page.height
                    page_width = first_page.width

                    # ЛЕВЫЙ верхний угол (шапка с номером и датой документа)
                    bbox_left = (
                        0,                      # x0 - левый край
                        0,                      # y0 - верхний край
                        page_width * 0.5,      # x1 - середина страницы
                        page_height * 0.3      # y1 - 30% от высоты
                    )

                    cropped_left = first_page.crop(bbox_left)
                    text_left = cropped_left.extract_text()

                    text_to_search = text_left or ""

            if not text_to_search or len(text_to_search.strip()) < 10:
                # Мало текста - пробуем OCR если доступен
                if OCR_AVAILABLE and not self.full_text:
                    print(f"Мало текста в шапке {self.filename}, используем OCR для извлечения номера...")
                    from ocr_parser import extract_text_from_pdf
                    text_to_search = extract_text_from_pdf(self.pdf_path)

            if not text_to_search:
                return result

            # Ищем номер исходящего документа
            # ВАЖНО: Исключаем "На №" (это входящий номер)
            # Паттерны для исходящего номера (более длинные форматы)
            outgoing_patterns = [
                r'№\s*(\d{2}-\d{2}-\d{4,5}/\d{2}-\d-\d)',   # №01-32-5399/26-0-0
                r'№\s*(\d{2}-\d{4,5}/\d{2}-\d-\d)',          # №01-5399/26-0-0
                r'(\d{2}-\d{2}-\d{4,5}/\d{2}-\d-\d)',        # Без № (рукописный)
            ]

            for pattern in outgoing_patterns:
                # Ищем все совпадения
                matches = re.finditer(pattern, text_to_search)
                for match in matches:
                    # Проверяем, что это не "На №" (входящий номер)
                    match_start = match.start()
                    prefix = text_to_search[max(0, match_start-10):match_start]
                    if 'На' not in prefix and 'на' not in prefix:
                        result['number'] = match.group(1).strip()
                        break
                if result['number']:
                    break

            # Если не нашли длинный номер, ищем короткий формат (только если не "На №")
            if not result['number']:
                short_pattern = r'№\s*(\d{3,4}[-/]\d+[-/]\d+-?\d*)'
                matches = re.finditer(short_pattern, text_to_search)

                for match in matches:
                    # Проверяем контекст перед номером (увеличиваем до 20 символов)
                    match_start = match.start()
                    prefix = text_to_search[max(0, match_start-20):match_start]

                    # Исключаем если перед номером есть "На " или "на " (с пробелом!)
                    if 'На ' not in prefix and 'на ' not in prefix:
                        result['number'] = match.group(1).strip()
                        break

            # Ищем дату
            # Примеры: от 14.01.2026, 14.01.2026
            date_patterns = [
                r'от\s+(\d{2}\.\d{2}\.\d{4})',      # от 14.01.2026
                r'№.*?(\d{2}\.\d{2}\.\d{4})',       # Дата рядом с номером
                r'(\d{2}\.\d{2}\.20\d{2})',         # Дата 2020-х годов
            ]

            for pattern in date_patterns:
                matches = re.finditer(pattern, text_to_search)
                for match in matches:
                    # Берем первую найденную дату
                    result['date'] = match.group(1).strip()
                    break
                if result['date']:
                    break

            return result

        except Exception as e:
            print(f"Ошибка при извлечении номера/даты из шапки: {e}")
            return result

    def parse_remarks_by_paragraphs(self) -> List[Dict[str, str]]:
        """
        Разбивка текста на замечания по абзацам.
        Улучшенный алгоритм для GOT-OCR:
        - Исправляет склеенные слова
        - Ищет нумерованные замечания (1., 2., п.1, п.2)
        - Объединяет многострочные замечания по смыслу
        - Разбивает по пустым строкам и точкам
        - Поддерживает различные маркеры начала и конца замечаний
        """
        if not self.full_text:
            self.extract_text()

        remarks = []

        # Исправляем склеенные слова (частая проблема GOT-OCR)
        text = self._fix_concatenated_words(self.full_text)

        # Ищем начало блока с замечаниями
        start_markers = [
            r"устранены не в полном объеме\.",
            r"сообщает следующее\.",
            r"сообщает\.",
            r"замечания[:\.]",
            r"выявлен ряд замечаний",
            r"По результат[а-я]+ проверки",
        ]

        start_pos = 0
        # ГРАДДОК (13.11): remarks in an appendix — «Приложение к письму …» then a heading line
        # «Замечания СПб ГБУ …»: they start right after that heading. Without this the first marker
        # found was a sub-heading in the middle («…учесть следующие замечания.») and the items above it were lost.
        appendix = re.search(r"Приложение\s+к\s+письму[^\n]*\n(?:[^\n]*\n){0,3}?\s*Замечани[^\n]*\n", text, re.IGNORECASE)
        if appendix:
            start_pos = appendix.end()
        for marker in ([] if appendix else start_markers):
            match = re.search(marker, text, re.IGNORECASE)
            if match:
                start_pos = match.end()
                break

        # Ищем конец блока с замечаниями
        end_markers = [
            r"В связи с изложенным",
            r"Учитывая вышеизложенное",
            r"Заместитель",
            r"Исходя из вышеизложенного",
            r"С уважением",
            r"Начальник Управления",
        ]

        end_pos = len(text)
        for marker in end_markers:
            # Ищем только ПОСЛЕ start_pos, чтобы не находить в начале документа
            match = re.search(marker, text[start_pos:], re.IGNORECASE)
            if match:
                end_pos = start_pos + match.start()
                break

        # Извлекаем текст с замечаниями
        remarks_text = text[start_pos:end_pos].strip()

        # Удаляем технические строки
        lines_to_skip = [
            "ПРАВИТЕЛЬСТВО",
            "КОМИТЕТ",
            "Караванная",
            "тел.",
            "E-mail",
            "http://",
            "На №",
            "__________",
        ]

        # Пытаемся найти нумерованные замечания (п.1, п.2, 1., 2. и т.д.)
        numbered_remarks = self._extract_numbered_remarks(remarks_text)

        if numbered_remarks:
            # Если нашли нумерованные замечания - используем их
            remarks = numbered_remarks
        else:
            # Иначе используем разбивку по абзацам
            # Разбиваем на строки
            lines = remarks_text.split('\n')
            clean_lines = []

            for line in lines:
                line = line.strip()
                # Пропускаем пустые и технические строки
                if not line or any(skip in line for skip in lines_to_skip):
                    continue
                clean_lines.append(line)

            # Объединяем строки в замечания
            current_remark = []

            for line in clean_lines:
                # Пропускаем строки-заголовки
                skip_full_lines = [
                    "Замечания, направленные",
                    "На рассмотрение представлены",
                    "Комитет рассмотрел",
                ]

                if any(skip in line for skip in skip_full_lines):
                    continue

                # Если строка начинается с заглавной буквы и есть накопленное замечание
                if line and line[0].isupper() and current_remark:
                    prev_line = current_remark[-1] if current_remark else ""

                    # Если предыдущая строка заканчивается точкой - это конец замечания
                    # ГРАДДОК: a dot inside an open «…» (a standard's title) does not end the remark
                    joined = ' '.join(current_remark)
                    if prev_line.rstrip().endswith('.') and joined.count('«') <= joined.count('»'):
                        remark_text = ' '.join(current_remark)
                        if len(remark_text) >= 50:  # Минимальная длина замечания
                            remarks.append({
                                "order_number": len(remarks) + 1,
                                "text": remark_text,
                                "char_count": len(remark_text),
                            })
                        current_remark = [line]
                    else:
                        # Продолжение предыдущего замечания
                        current_remark.append(line)
                else:
                    current_remark.append(line)

            # Добавляем последнее замечание
            if current_remark:
                remark_text = ' '.join(current_remark)
                if len(remark_text) >= 50:
                    remarks.append({
                        "order_number": len(remarks) + 1,
                        "text": remark_text,
                        "char_count": len(remark_text),
                    })

        self.remarks = remarks
        return remarks

    def _fix_concatenated_words(self, text: str) -> str:
        """
        Исправляет склеенные слова, характерные для GOT-OCR.
        Например: ПРАВИТЕЛЬСТВОСАНКТ-ПЕТЕРБУРГА → ПРАВИТЕЛЬСТВО САНКТ-ПЕТЕРБУРГА
        """
        # Добавляем пробелы перед заглавными буквами, если они идут после строчных
        # Пример: СанктПетербург → Санкт Петербург
        text = re.sub(r'([а-яёa-z])([А-ЯЁA-Z])', r'\1 \2', text)

        # Известные склеенные слова
        replacements = {
            'ПРАВИТЕЛЬСТВОСАНКТ-ПЕТЕРБУРГА': 'ПРАВИТЕЛЬСТВО САНКТ-ПЕТЕРБУРГА',
            'ПРАВИТЕЛЬСТВОСАНКТ': 'ПРАВИТЕЛЬСТВО САНКТ',
            'КОМИТЕТПО': 'КОМИТЕТ ПО',
            'ОХРАНЕОКРУЖАЮЩЕЙ': 'ОХРАНЕ ОКРУЖАЮЩЕЙ',
            'СРЕДЫИОБЕСПЕЧЕНИЮ': 'СРЕДЫ И ОБЕСПЕЧЕНИЮ',
        }

        for old, new in replacements.items():
            text = text.replace(old, new)

        return text

    def _extract_numbered_remarks(self, text: str) -> List[Dict[str, str]]:
        """
        Извлекает нумерованные замечания (п.1, п.2, 1., 2. и т.д.)
        """
        remarks = []

        # Паттерны для нумерации замечаний
        patterns = [
            r'(?:^|\n)(\d+)\.\s+([^\n]+(?:\n(?!\d+\.)[^\n]+)*)',  # 1. Текст замечания
            r'(?:^|\n)п\.?\s*(\d+)\.?\s+([^\n]+(?:\n(?!п\.?\s*\d+)[^\n]+)*)',  # п.1 Текст замечания
            r'(?:^|\n)§\s*(\d+)\.\s+([^\n]+(?:\n(?!§\s*\d+)[^\n]+)*)',  # §1. Текст замечания
        ]

        for pattern in patterns:
            matches = re.finditer(pattern, text, re.MULTILINE | re.DOTALL)
            temp_remarks = []

            for match in matches:
                number = int(match.group(1))
                remark_text = match.group(2).strip()

                # Очищаем текст от лишних переносов
                remark_text = ' '.join(remark_text.split())

                if len(remark_text) >= 30:  # Минимальная длина
                    temp_remarks.append({
                        "order_number": number,
                        "text": remark_text,
                        "char_count": len(remark_text),
                    })

            # Если нашли замечания с этим паттерном - используем их
            if len(temp_remarks) >= 2:  # Минимум 2 замечания
                remarks = temp_remarks
                break

        return remarks
