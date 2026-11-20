from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SCRIPTS = ROOT / "seed" / "scripts"
SCHEDULE_XLSX = SAMPLES / "EAMS_Schedule_template_2.xlsx"
WAREHOUSE_XLSX = SAMPLES / "Eams_New_Warehouse_Registration_Template.xlsx"
EMPLOYEE_XLSX = SAMPLES / "EAMS_NEW_EMPLOYEE_TEMPLATE.xlsx"


def script(name: str) -> str:
    return (SCRIPTS / name).read_text()
