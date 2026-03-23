@echo off
setlocal enabledelayedexpansion
title Push to GitHub

cd /d "%~dp0"

REM ── Check git ─────────────────────────────────────────────────────────────
git --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: git is not installed or not on PATH.
    pause
    exit /b 1
)

REM ── Show current status ───────────────────────────────────────────────────
echo.
echo =========================================
echo   Jira to ADO Pipeline - Push to GitHub
echo =========================================
echo.
git status --short
echo.

REM ── Check for changes ─────────────────────────────────────────────────────
git diff --quiet HEAD 2>nul
git diff --cached --quiet HEAD 2>nul
git status --porcelain >nul 2>&1
for /f "tokens=*" %%i in ('git status --porcelain') do (
    set HAS_CHANGES=1
)

REM ── Get current version from latest tag ───────────────────────────────────
for /f "tokens=*" %%i in ('git tag --sort=-version:refname 2^>nul') do (
    set LAST_TAG=%%i
    goto :got_tag
)
:got_tag

if not defined LAST_TAG (
    set LAST_TAG=v0.0.0
    echo No existing version tags found. Starting from v0.0.0
) else (
    echo Current version: !LAST_TAG!
)

REM ── Parse version number ──────────────────────────────────────────────────
set VERSION_STR=!LAST_TAG!
set VERSION_STR=!VERSION_STR:v=!

for /f "tokens=1,2,3 delims=." %%a in ("!VERSION_STR!") do (
    set MAJOR=%%a
    set MINOR=%%b
    set PATCH=%%c
)

if not defined MAJOR set MAJOR=0
if not defined MINOR set MINOR=0
if not defined PATCH set PATCH=0

REM ── Choose bump type ──────────────────────────────────────────────────────
echo.
echo Version bump type:
echo   [1] Patch  (bug fixes)          v!MAJOR!.!MINOR!.!PATCH! → v!MAJOR!.!MINOR!.x
echo   [2] Minor  (new features)       v!MAJOR!.!MINOR!.!PATCH! → v!MAJOR!.x.0
echo   [3] Major  (breaking changes)   v!MAJOR!.!MINOR!.!PATCH! → vx.0.0
echo   [4] Skip versioning (no tag)
echo.
set /p BUMP_CHOICE="Choose [1/2/3/4]: "

if "!BUMP_CHOICE!"=="1" (
    set /a NEW_PATCH=!PATCH!+1
    set NEW_VERSION=v!MAJOR!.!MINOR!.!NEW_PATCH!
) else if "!BUMP_CHOICE!"=="2" (
    set /a NEW_MINOR=!MINOR!+1
    set NEW_VERSION=v!MAJOR!.!NEW_MINOR!.0
) else if "!BUMP_CHOICE!"=="3" (
    set /a NEW_MAJOR=!MAJOR!+1
    set NEW_VERSION=v!NEW_MAJOR!.0.0
) else if "!BUMP_CHOICE!"=="4" (
    set NEW_VERSION=
) else (
    echo Invalid choice. Defaulting to patch bump.
    set /a NEW_PATCH=!PATCH!+1
    set NEW_VERSION=v!MAJOR!.!MINOR!.!NEW_PATCH!
)

if defined NEW_VERSION (
    echo.
    echo New version: !NEW_VERSION!
)

REM ── Commit message ────────────────────────────────────────────────────────
echo.
if defined NEW_VERSION (
    set /p COMMIT_MSG="Commit message (default: Release !NEW_VERSION!): "
    if "!COMMIT_MSG!"=="" set COMMIT_MSG=Release !NEW_VERSION!
) else (
    set /p COMMIT_MSG="Commit message: "
    if "!COMMIT_MSG!"=="" (
        echo No commit message entered. Aborting.
        pause
        exit /b 1
    )
)

REM ── Stage and commit ──────────────────────────────────────────────────────
echo.
echo Staging all changes...
git add -A

echo Committing: "!COMMIT_MSG!"
git commit -m "!COMMIT_MSG!"
if errorlevel 1 (
    echo.
    echo Nothing to commit or commit failed.
)

REM ── Push to GitHub ────────────────────────────────────────────────────────
echo.
echo Pushing to GitHub...
git push origin main
if errorlevel 1 (
    echo.
    echo ERROR: Push failed. Check your GitHub credentials or connection.
    pause
    exit /b 1
)

REM ── Create and push version tag ───────────────────────────────────────────
if defined NEW_VERSION (
    echo.
    echo Tagging as !NEW_VERSION!...
    git tag -a !NEW_VERSION! -m "Release !NEW_VERSION!"
    git push origin !NEW_VERSION!
    if errorlevel 1 (
        echo WARNING: Tag push failed, but code was pushed successfully.
    ) else (
        echo Tag !NEW_VERSION! pushed successfully.
    )
)

REM ── Done ──────────────────────────────────────────────────────────────────
echo.
echo =========================================
if defined NEW_VERSION (
    echo   Done! Pushed and tagged as !NEW_VERSION!
) else (
    echo   Done! Changes pushed to GitHub.
)
echo   Railway will auto-deploy within ~2 minutes.
echo =========================================
echo.
pause
