"""Чистая генерация матрицы QR-кода (для отрисовки в Qt).

qr_matrix(data) -> list[list[bool]] — квадратная матрица модулей QR.
Отрисовка в QPixmap живёт в GUI; здесь — только данные (тестируется).
"""
import qrcode


def qr_matrix(data: str):
    """Вернуть матрицу QR-кода (список списков bool) для строки data."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(data or "")
    qr.make(fit=True)
    return qr.get_matrix()
