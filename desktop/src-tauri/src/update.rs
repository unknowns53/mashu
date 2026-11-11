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

use crate::setup::repository_url;
use crate::{drop_process, Backend};

#[derive(Default, Serialize, Deserialize)]
pub(crate) struct Config {
    pub(crate) checkout: Option<PathBuf>,
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

pub(crate) fn write_config(app: &AppHandle, config: &Config) -> Result<(), String> {
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

/// The checkout that holds a mashu executable, found by following it out of `.venv`.
/// The README links ~/.local/bin/mashu to a checkout's environment, so a release build
/// finds the checkout without being told.
fn checkout_of(executable: &Path) -> Option<PathBuf> {
    let real = executable.canonicalize().ok()?;
    let venv = real
        .ancestors()
        .find(|dir| dir.file_name() == Some(".venv".as_ref()))?;
    let root = venv.parent()?;
    root.join(".git").exists().then(|| root.to_path_buf())
}

/// The checkout a local build was made from, when that checkout is still on this machine.
fn built_checkout() -> Option<PathBuf> {
    let built = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..");
    built
        .join(".git")
        .exists()
        .then(|| built.canonicalize().unwrap_or(built))
}

/// A debug build runs from the checkout it was built from, so a window built in a worktree
/// talks to that worktree's backend. The Settings screen's checkout is shared with the
/// installed app through the bundle identifier and usually names the main checkout, whose
/// backend may not yet answer what this build's pages ask. Other builds take the checkout set
/// on the Settings screen, else the one a local build was made from, else the one the mashu on
/// PATH runs from.
pub fn checkout(app: &AppHandle) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        if let Some(root) = built_checkout() {
            return Some(root);
        }
    }
    if let Some(root) = read_config(app).checkout {
        return Some(root);
    }
    built_checkout().or_else(|| tool("mashu").and_then(|found| checkout_of(&found)))
}

/// The mashu executable: MASHU_EXE, then the checkout's virtual environment, then PATH.
/// None when there is none to start.
pub fn executable(app: &AppHandle) -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("MASHU_EXE") {
        return Some(PathBuf::from(path));
    }
    match checkout(app).map(|root| venv_mashu(&root)) {
        Some(local) if local.is_file() => Some(local),
        _ => tool("mashu"),
    }
}

/// A command that starts the mashu at `program`.
///
/// On Windows a virtual environment's mashu.exe is started as python.exe -m mashu from the
/// same folder. uv sync rewrites mashu.exe at every version change and cannot while a process
/// runs it, because a running mashu.exe can be neither deleted nor renamed; uv leaves
/// python.exe alone unless the Python version changes.
pub fn mashu_command(program: &Path) -> Command {
    let python = program.with_file_name("python.exe");
    let venv_mashu = program
        .file_name()
        .is_some_and(|name| name.eq_ignore_ascii_case("mashu.exe"));
    if cfg!(windows) && venv_mashu && python.is_file() {
        let mut command = Command::new(python);
        command.args(["-m", "mashu"]);
        return command;
    }
    Command::new(program)
}

/// A path as typed, with a leading ~/ read as the home directory.
pub(crate) fn expand(path: &str) -> PathBuf {
    let path = path.trim();
    match (path.strip_prefix("~/"), home()) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => PathBuf::from(path),
    }
}

pub(crate) fn home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

/// A program by name, from PATH or where its installer puts it. An app started from the
/// Dock or the Start menu gets a short PATH without ~/.local/bin or ~/.cargo/bin.
pub(crate) fn tool(name: &str) -> Option<PathBuf> {
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

pub(crate) fn run(program: &Path, args: &[&str], root: &Path) -> Result<String, String> {
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

/// The newest release tag a remote has; prereleases are never offered.
pub(crate) fn latest_release(
    git: &Path,
    dir: &Path,
    remote: &str,
) -> Result<Option<String>, String> {
    let refs = run(git, &["ls-remote", "--tags", "--refs", remote, "v*"], dir)?;
    Ok(refs
        .lines()
        .filter_map(|line| line.rsplit('/').next())
        .filter_map(|tag| version_key(tag).map(|key| (key, tag.trim_start_matches('v'))))
        .filter(|(key, _)| key.3)
        .max_by_key(|(key, _)| *key)
        .map(|(_, version)| version.to_owned()))
}

/// The version the backend runs and the checkout it reports running from.
fn backend_version(app: &AppHandle) -> Result<(String, Option<PathBuf>), String> {
    let reply = app.state::<Backend>().request(app, "version", json!({}))?;
    if reply["ok"] != json!(true) {
        return Err(reply["error"].as_str().unwrap_or("refused").to_owned());
    }
    let version = reply["result"]["version"]
        .as_str()
        .ok_or("the backend gave no version")?;
    let root = reply["result"]["checkout"].as_str().map(PathBuf::from);
    Ok((version.to_owned(), root))
}

/// The checkout an update moves: the one the app knows, else the one the backend runs from.
fn update_root(app: &AppHandle) -> Option<PathBuf> {
    checkout(app).or_else(|| backend_version(app).ok().and_then(|(_, root)| root))
}

#[tauri::command]
pub fn checkout_get(app: AppHandle) -> Value {
    let executable = executable(&app);
    json!({
        "path": checkout(&app),
        "set": read_config(&app).checkout.is_some(),
        "ready": executable.as_ref().is_some_and(|path| path.is_file()),
        "executable": executable,
        "from_env": std::env::var_os("MASHU_EXE").is_some(),
        "from_build": cfg!(debug_assertions) && built_checkout().is_some(),
    })
}

/// Point the app at a checkout; the backend restarts from it on the next request.
#[tauri::command]
pub fn checkout_set(app: AppHandle, path: String) -> Result<Value, String> {
    let root = expand(&path);
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
    restart_backend(&app)?;
    Ok(checkout_get(app.clone()))
}

/// The app's and the checkout's versions, and the newest release when it is newer than either.
#[tauri::command]
pub async fn update_check(app: AppHandle) -> Result<Value, String> {
    let app_version = app.package_info().version.to_string();
    let local = app.clone();
    let (backend, root, latest) = tauri::async_runtime::spawn_blocking(move || {
        let backend = backend_version(&local);
        let reported = backend.as_ref().ok().and_then(|(_, root)| root.clone());
        let root = checkout(&local).or(reported);
        // Without a checkout, the repository a fresh install would clone still names the releases.
        let latest = match (tool("git"), &root) {
            (None, _) => Err("git was not found".to_owned()),
            (Some(git), Some(root)) => latest_release(&git, root, "origin"),
            (Some(git), None) => latest_release(&git, &std::env::temp_dir(), &repository_url()),
        };
        (backend, root, latest)
    })
    .await
    .map_err(|error| error.to_string())?;
    // Offline, the versions on this machine are still worth showing.
    let (latest, error) = match latest {
        Ok(latest) => (latest, None),
        Err(error) => (None, Some(error)),
    };
    let (mashu, mashu_error) = match backend {
        Ok((version, _)) => (Some(version), None),
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
        "mashu_error": mashu_error,
        "checkout": root,
        "latest": latest,
        "available": available,
        "dev_build": cfg!(debug_assertions),
        "error": error,
    }))
}

fn step(app: &AppHandle, name: &str) {
    let _ = app.emit("update-step", name);
}

/// Stop the backend so the next request starts it from the checkout now configured.
pub(crate) fn restart_backend(app: &AppHandle) -> Result<(), String> {
    let backend = app.state::<Backend>();
    let mut connection = backend
        .0
        .lock()
        .map_err(|_| "the backend lock was poisoned")?;
    drop_process(&mut connection);
    Ok(())
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
        format!("{error}\nOn Windows an agent that starts .venv\\Scripts\\mashu.exe holds it while it runs; close that agent's sessions and update again, and register its MCP server as .venv\\Scripts\\python.exe -m mashu serve so later updates need not close them.")
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
    let root = update_root(&app).ok_or("set where the Mashu checkout is first")?;
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
