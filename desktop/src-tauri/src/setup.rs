//! Getting Mashu onto a machine that has only this app: a checkout of the latest release, its
//! environment, and its database. Registering the MCP server and hooks with each agent is
//! left to the README, since that edits the agents' own settings.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

use crate::update::{
    checkout, expand, home, latest_release, restart_backend, run, tool, venv_mashu, write_config,
    Config,
};

/// The repository this app was built from, so a fork's app installs the fork.
fn repository_url() -> String {
    let repository = option_env!("GITHUB_REPOSITORY").unwrap_or("unknowns53/mashu");
    format!("https://github.com/{repository}.git")
}

/// Where PostgreSQL's installers put its programs, which an app's PATH rarely holds.
fn postgres_dirs() -> Vec<PathBuf> {
    let parents: &[(&str, &str)] = if cfg!(windows) {
        &[(r"C:\Program Files\PostgreSQL", "")]
    } else {
        &[
            ("/opt/homebrew/opt", "postgresql"),
            ("/usr/local/opt", "postgresql"),
            ("/Applications/Postgres.app/Contents/Versions", ""),
        ]
    };
    let mut dirs = Vec::new();
    for (parent, prefix) in parents {
        let Ok(entries) = std::fs::read_dir(parent) else {
            continue;
        };
        let mut found: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with(prefix))
            .map(|entry| entry.path().join("bin"))
            .collect();
        // The newest version first, so a version named in its folder beats an older one.
        found.sort();
        found.reverse();
        dirs.extend(found);
    }
    dirs
}

fn postgres_tool(name: &str) -> Option<PathBuf> {
    tool(name).or_else(|| {
        let file = if cfg!(windows) {
            format!("{name}.exe")
        } else {
            name.to_owned()
        };
        postgres_dirs()
            .into_iter()
            .map(|dir| dir.join(&file))
            .find(|path| path.is_file())
    })
}

fn default_root() -> PathBuf {
    home().unwrap_or_default().join("mashu")
}

fn is_free(root: &Path) -> bool {
    match std::fs::read_dir(root) {
        Ok(mut entries) => entries.next().is_none(),
        Err(_) => !root.exists(),
    }
}

fn step(app: &AppHandle, name: &str) {
    let _ = app.emit("setup-step", name);
}

/// What installing into `path` needs, and which of it this machine has.
#[tauri::command]
pub fn setup_check(path: Option<String>) -> Value {
    let root = path
        .filter(|path| !path.trim().is_empty())
        .map(|path| expand(&path))
        .unwrap_or_else(default_root);
    let running = postgres_tool("pg_isready")
        .is_some_and(|program| run(&program, &[], &home().unwrap_or_default()).is_ok());
    json!({
        "path": root,
        "free": is_free(&root),
        "git": tool("git").is_some(),
        "uv": tool("uv").is_some(),
        "postgres": postgres_tool("createdb").is_some(),
        "postgres_running": running,
    })
}

/// Create the database the backend connects to by default, then bring its schema up to date.
fn create_database(root: &Path) -> Result<(), String> {
    let createdb = postgres_tool("createdb").ok_or("createdb was not found; install PostgreSQL")?;
    if let Err(error) = run(&createdb, &["mashu"], root) {
        if !error.contains("already exists") {
            return Err(error);
        }
    }
    run(&venv_mashu(root), &["admin", "migrate"], root)?;
    Ok(())
}

fn install(app: &AppHandle, root: &Path) -> Result<(), String> {
    let git = tool("git").ok_or("git was not found")?;
    let uv = tool("uv").ok_or("uv was not found")?;
    if !is_free(root) {
        return Err(format!("{} is not empty", root.display()));
    }
    let parent = root.parent().ok_or("choose a folder inside another one")?;
    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let url = repository_url();

    step(app, "clone");
    let version = latest_release(&git, parent, &url)?.ok_or("the repository has no release")?;
    let target = root.to_string_lossy();
    run(&git, &["clone", "--quiet", &url, &target], parent)?;
    // main stays the branch, at the release, so an update later fast-forwards it to the next one.
    run(
        &git,
        &["reset", "--quiet", "--hard", &format!("v{version}")],
        root,
    )?;

    step(app, "sync");
    run(&uv, &["sync", "--extra", "mcp"], root)?;

    step(app, "database");
    create_database(root)?;

    write_config(
        app,
        &Config {
            checkout: Some(root.canonicalize().unwrap_or(root.to_path_buf())),
        },
    )?;
    restart_backend(app)
}

/// Clone the latest release into an empty folder, sync it, and create its database.
#[tauri::command]
pub async fn setup_install(app: AppHandle, path: String) -> Result<(), String> {
    let root = expand(&path);
    let local = app.clone();
    tauri::async_runtime::spawn_blocking(move || install(&local, &root))
        .await
        .map_err(|error| error.to_string())?
}

/// Create the database for the checkout in use, when it is the only thing missing.
#[tauri::command]
pub async fn setup_database(app: AppHandle) -> Result<(), String> {
    let root = checkout(&app).ok_or("set where the Mashu checkout is first")?;
    let local = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        create_database(&root)?;
        restart_backend(&local)
    })
    .await
    .map_err(|error| error.to_string())?
}
