from datetime import datetime


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def filename_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")
