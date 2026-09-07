import io

import exifread
from flask import Flask, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 МБ лимит на файл


def _dms_to_decimal(dms, ref) -> float:
    """Конвертирует GPS-координаты EXIF (град/мин/сек) в десятичные."""
    degrees = float(dms.values[0].num) / float(dms.values[0].den)
    minutes = float(dms.values[1].num) / float(dms.values[1].den)
    seconds = float(dms.values[2].num) / float(dms.values[2].den)
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        result = -result
    return result


def extract_exif(file_bytes: bytes) -> list[str]:
    """Извлекает EXIF-теги и GPS, возвращает готовый список строк для вывода."""
    tags = exifread.process_file(io.BytesIO(file_bytes), details=False)

    if not tags:
        return ["ℹ️ EXIF-данные отсутствуют."]

    interesting = {
        "Image Make": "Производитель устройства",
        "Image Model": "Модель устройства",
        "EXIF DateTimeOriginal": "Дата/время съёмки",
        "EXIF ExposureTime": "Выдержка",
        "EXIF FNumber": "Диафрагма",
        "EXIF ISOSpeedRatings": "ISO",
        "EXIF FocalLength": "Фокусное расстояние",
        "Image Software": "ПО обработки",
    }
    lines = [f"{label}: {tags[tag]}" for tag, label in interesting.items() if tag in tags]

    gps_lat = tags.get("GPS GPSLatitude")
    gps_lat_ref = tags.get("GPS GPSLatitudeRef")
    gps_lon = tags.get("GPS GPSLongitude")
    gps_lon_ref = tags.get("GPS GPSLongitudeRef")

    if gps_lat and gps_lon and gps_lat_ref and gps_lon_ref:
        lat = _dms_to_decimal(gps_lat, str(gps_lat_ref))
        lon = _dms_to_decimal(gps_lon, str(gps_lon_ref))
        lines.append(f"GPS: {lat:.6f}, {lon:.6f}")
        lines.append(f"Карта: https://www.google.com/maps?q={lat},{lon}")

    return lines or ["ℹ️ Только служебные теги без содержательной информации."]


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html", lines=None)

    file = request.files.get("photo")
    if not file or file.filename == "":
        return render_template("index.html", lines=None, error="Файл не выбран.")

    lines = extract_exif(file.read())
    return render_template("index.html", lines=lines, filename=file.filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
