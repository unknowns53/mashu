//! Where the Mashu checkout is, and moving it and the app together to a newer release.
//!
//! The app is a release build that knows nothing of the machine, while the agents' MCP server
//! and hooks run from a git checkout. An update fast-forwards that checkout's main to the
//! release tag, syncs its environment and schema, and only then replaces the app, so the
//! window never runs against an older backend than itself.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;

use crate::{drop_process, Backend};

#[derive(Default, Serialize, Deserialize)]
struct Config {
    checkout: Option<PathBuf>,
}

fn config_file(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|error| error.to_string())?;
    Ok(dir.join("config.json"))
}

fn read_config(app: &AppHandle) -> Config {
    config_file(app)
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn write_config(app: &AppHandle, config: &Config) -> Result<(), String> {
    let path = config_file(app)?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|error| error.to_string())?;
    }
    let text = serde_json::to_string_pretty(config).map_err(|error| error.to_string())?;
    std::fs::write(path, text).map_err(|error| error.to_string())
}

/// The mashu executable inside a checkout's virtual environment.
pub fn venv_mashu(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("mashu.exe")
    } else {
        root.join(".venv").join("bin").join("mashu")
    }
}

/// The checkout set on the Settings screen, else the one a local build was made from.
pub fn checkout(app: &AppHandle) -> Option<PathBuf> {
    if let Some(root) = read_config(app).checkout {
        return Some(root);
    }
    let built = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..");
    built
        .join(".git")
        .exists()
        .then(|| built.canonicalize().unwrap_or(built))
}

/// The mashu executable: MASHU_EXE, then the checkout's virtual environment, then PATH.
pub fn executable(app: &AppHandle) -> PathBuf {
    if let Some(path) = std::env::var_os("MASHU_EXE") {
        return PathBuf::from(path);
    }
    match checkout(app).map(|root| venv_mashu(&root)) {
        Some(local) if local.is_file() => local,
        _ => PathBuf::from("mashu"),
    }
}

fn home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

/// A program by name, from PATH or where its installer puts it. An app started from the
/// Dock or the Start menu gets a short PATH without ~/.local/bin or ~/.cargo/bin.
fn tool(name: &str) -> Option<PathBuf> {
    let file = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    };
    let mut dirs: Vec<PathBuf> = std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).collect())
        .unwrap_or_default();
    if let Some(home) = home() {
        dirs.push(home.join(".local").join("bin"));
        dirs.push(home.join(".cargo").join("bin"));
    }
    dirs.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"].map(PathBuf::from));
    dirs.into_iter()
        .map(|dir| dir.join(&file))
        .find(|path| path.is_file())
}

fn run(program: &Path, args: &[&str], root: &Path) -> Result<String, String> {
    let mut command = Command::new(program);
    command.args(args).current_dir(root);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: no console window flashes up behind the app.
        command.creation_flags(0x0800_0000);
    }
    let output = command
        .output()
        .map_err(|error| format!("could not run {}: {error}", program.display()))?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
    } else {
        let name = program.file_name().unwrap_or_default().to_string_lossy();
        let said = String::from_utf8_lossy(&output.stderr);
        Err(format!("{name} {} failed: {}", args.join(" "), said.trim()))
    }
}

/// (major, minor, patch, is_release) of "4.1.0" or "4.1.0b1"; a prerelease sorts below its release.
fn version_key(text: &str) -> Option<(u64, u64, u64, bool)> {
    let text = text.trim().trim_start_matches('v');
    let end = text
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(text.len());
    let mut parts = text[..end].split('.').map(|part| part.parse::<u64>().ok());
    let (major, minor, patch) = (parts.next()??, parts.next()??, parts.next()??);
    Some((major, minor, patch, end == text.len()))
}

/// The newest release tag the checkout's origin has; prereleases are never offered.
fn latest_release(git: &Path, root: &Path) -> Result<Option<String>, String> {
    let refs = run(
        git,
        &["ls-remote", "--tags", "--refs", "origin", "v*"],
        root,
    )?;
    Ok(refs
        .lines()
        .filter_map(|line| line.rsplit('/').next())
        .filter_map(|tag| version_key(tag).map(|key| (key, tag.trim_start_matches('v'))))
        .filter(|(key, _)| key.3)
        .max_by_key(|(key, _)| *key)
        .map(|(_, version)| version.to_owned()))
}

fn backend_version(app: &AppHandle) -> Option<String> {
    let reply = app
        .state::<Backend>()
        .request(app, "version", json!({}))
        .ok()?;
    reply["result"]["version"].as_str().map(str::to_owned)
}

#[tauri::command]
pub fn checkout_get(app: AppHandle) -> Value {
    json!({
        "path": checkout(&app),
        "set": read_config(&app).checkout.is_some(),
        "executable": executable(&app),
        "from_env": std::env::var_os("MASHU_EXE").is_some(),
    })
}

/// Point the app at a checkout; the backend restarts from it on the next request.
#[tauri::command]
pub fn checkout_set(app: AppHandle, path: String) -> Result<Value, String> {
    let path = path.trim();
    let root = match (path.strip_prefix("~/"), home()) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => PathBuf::from(path),
    };
    if !root.join(".git").exists() {
        return Err(format!("{} is not a git checkout of Mashu", root.display()));
    }
    if !venv_mashu(&root).is_file() {
        return Err(format!(
            "{} has no {}; run uv sync --extra mcp there first",
            root.display(),
            venv_mashu(Path::new(".")).display()
        ));
    }
    write_config(
        &app,
        &Config {
            checkout: Some(root.canonicalize().unwrap_or(root)),
        },
    )?;
    let backend = app.state::<Backend>();
    let mut connection = backend
        .0
        .lock()
        .map_err(|_| "the backend lock was poisoned")?;
    drop_process(&mut connection);
    Ok(checkout_get(app.clone()))
}

/// The app's and the checkout's versions, and the newest release when it is newer than either.
#[tauri::command]
pub async fn update_check(app: AppHandle) -> Result<Value, String> {
    let app_version = app.package_info().version.to_string();
    let local = app.clone();
    let (mashu, latest) = tauri::async_runtime::spawn_blocking(move || {
        let mashu = backend_version(&local);
        let latest = match (tool("git"), checkout(&local)) {
            (Some(git), Some(root)) => latest_release(&git, &root),
            _ => Ok(None),
        };
        (mashu, latest)
    })
    .await
    .map_err(|error| error.to_string())?;
    // Offline, the versions on this machine are still worth showing.
    let (latest, error) = match latest {
        Ok(latest) => (latest, None),
        Err(error) => (None, Some(error)),
    };
    let newer = |current: Option<&str>| match (&latest, current.and_then(version_key)) {
        (Some(latest), Some(current)) => version_key(latest).is_some_and(|key| key > current),
        (Some(_), None) => true,
        _ => false,
    };
    let available = newer(Some(&app_version)) || newer(mashu.as_deref());
    Ok(json!({
        "app": app_version,
        "mashu": mashu,
        "latest": latest,
        "available": available,
        "dev_build": cfg!(debug_assertions),
        "error": error,
    }))
}

fn step(app: &AppHandle, name: &str) {
    let _ = app.emit("update-step", name);
}

/// Move the checkout to the release, holding the backend stopped so nothing runs half updated.
fn update_checkout(app: &AppHandle, root: &Path, version: &str) -> Result<(), String> {
    let git = tool("git").ok_or("git was not found")?;
    let tag = format!("v{version}");
    let backend = app.state::<Backend>();
    let mut connection = backend
        .0
        .lock()
        .map_err(|_| "the backend lock was poisoned")?;
    drop_process(&mut connection);

    step(app, "fetch");
    let changed = run(
        &git,
        &["status", "--porcelain", "--untracked-files=no"],
        root,
    )?;
    if !changed.is_empty() {
        return Err(format!(
            "the checkout at {} has uncommitted changes; commit or discard them first:\n{changed}",
            root.display()
        ));
    }
    let branch = run(&git, &["rev-parse", "--abbrev-ref", "HEAD"], root)?;
    if branch != "main" {
        return Err(format!(
            "the checkout at {} is on {branch}; switch it to main first",
            root.display()
        ));
    }
    run(&git, &["fetch", "--quiet", "--tags", "origin"], root)?;
    let target = format!("refs/tags/{tag}^{{commit}}");
    run(&git, &["rev-parse", "--verify", "--quiet", &target], root)
        .map_err(|_| format!("origin has no tag {tag}"))?;
    if run(&git, &["merge-base", "--is-ancestor", "HEAD", &tag], root).is_err() {
        return Err(format!(
            "main at {} has commits that {tag} does not; bring it up to date with git",
            root.display()
        ));
    }

    step(app, "checkout");
    run(&git, &["merge", "--ff-only", "--quiet", &tag], root)?;
    step(app, "sync");
    let uv = tool("uv").ok_or("uv was not found")?;
    run(&uv, &["sync", "--extra", "mcp"], root).map_err(|error| {
        format!("{error}\nA running MCP server can hold the files on Windows; close agent sessions and update again.")
    })?;
    step(app, "migrate");
    run(&venv_mashu(root), &["admin", "migrate"], root)?;
    Ok(())
}

/// Update the checkout, then the app, then restart into the new app.
///
/// Every step can run again, so after a failure the same update is simply started again.
#[tauri::command]
pub async fn update_install(app: AppHandle, version: String) -> Result<Value, String> {
    let root = checkout(&app).ok_or("set where the Mashu checkout is first")?;
    let local = app.clone();
    let target = version.clone();
    tauri::async_runtime::spawn_blocking(move || update_checkout(&local, &root, &target))
        .await
        .map_err(|error| error.to_string())??;
    if cfg!(debug_assertions) {
        step(&app, "done");
        return Ok(json!({"restart": false, "app": "dev_build"}));
    }
    step(&app, "app");
    let updater = app.updater().map_err(|error| error.to_string())?;
    match updater.check().await.map_err(|error| error.to_string())? {
        Some(update) if update.version == version => {
            update
                .download_and_install(|_, _| {}, || {})
                .await
                .map_err(|error| error.to_string())?;
            app.restart();
        }
        _ => {
            step(&app, "done");
            Ok(json!({"restart": false, "app": "not_published"}))
        }
    }
}
