# הכנת סביבת Claude Code למערכת המסחר

## מיקום מומלץ

`D:\Projects\TradingSystem`

ה-Baseline יישמר בתוך:

`D:\Projects\TradingSystem\docs\baseline-v0.5`

## שימוש

1. הורד את חבילת ה-Baseline v0.5 לתיקיית Downloads.
2. פתח PowerShell בתיקיית Starter זו.
3. הרץ:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\SETUP_PROJECT_WINDOWS.ps1
```

4. פתח את הפרויקט:

```powershell
cd D:\Projects\TradingSystem
code .
```

5. הפעל Claude Code מתוך שורש הפרויקט:

```powershell
claude
```

6. הדבק את תוכן `START_PROMPT_PHASE_0_HE.md`.
7. לאחר ש-Claude מציג תכנית ואתה מאשר אותה, הדבק את תוכן
   `IMPLEMENT_FIRST_TASK_PROMPT_HE.md`.

אין להעתיק Secrets, Sessions או חשבונות אמיתיים לתיקיית הפרויקט.
