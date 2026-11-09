//! A tray app around `mashu desktop-backend`: the window, the tray icon, and notices.
//!
//! Every decision is made by the Python child process through the same functions as the
//! terminal UI; this side only carries requests to it and shows what comes back.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, WindowEvent};
use tauri_plugin_notification::NotificationExt;

mod update;

/// How often the store is asked whether anything new is waiting.
const POLL: Duration = Duration::from_secs(60);
const TRAY_ID: &str = "mashu";

struct Process {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

#[derive(Default)]
struct Connection {
    process: Option<Process>,
    next_id: u64,
}

/// The one child process, shared by the window and the poller, one request at a time.
#[derive(Default)]
struct Backend(Mutex<Connection>);

/// Wakes the poller before its interval is up.
struct Wake(Sender<()>);

fn spawn(app: &AppHandle) -> Result<Process, String> {
    let program = update::executable(app);
    let mut command = Command::new(&program);
    command
        .arg("desktop-backend")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: no console window flashes up behind the app.
        command.creation_flags(0x0800_0000);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("could not start {}: {error}", program.display()))?;
    let stdin = child.stdin.take().ok_or("the backend has no stdin")?;
    let stdout = BufReader::new(child.stdout.take().ok_or("the backend has no stdout")?);
    Ok(Process {
        child,
        stdin,
        stdout,
    })
}

impl Backend {
    /// Send one request and wait for its reply line.
    ///
    /// A request that could not be sent is sent once more to a fresh process. One that was
    /// sent but got no answer is not, since it may already have been carried out.
    fn request(&self, app: &AppHandle, op: &str, args: Value) -> Result<Value, String> {
        let mut connection = self.0.lock().map_err(|_| "the backend lock was poisoned")?;
        for attempt in 0..2 {
            if connection.process.is_none() {
                connection.process = Some(spawn(app)?);
            }
            connection.next_id += 1;
            let id = connection.next_id;
            let line = json!({"id": id, "op": op, "args": args}).to_string() + "\n";
            let process = connection.process.as_mut().expect("spawned above");
            let sent = process
                .stdin
                .write_all(line.as_bytes())
                .and_then(|()| process.stdin.flush());
            if let Err(error) = sent {
                drop_process(&mut connection);
                if attempt == 1 {
                    return Err(format!("could not reach the backend: {error}"));
                }
                continue;
            }
            let mut reply = String::new();
            let read = process.stdout.read_line(&mut reply);
            return match read {
                Ok(0) | Err(_) => {
                    drop_process(&mut connection);
                    Err("the backend stopped before answering".into())
                }
                Ok(_) => {
                    let value: Value = serde_json::from_str(&reply)
                        .map_err(|error| format!("unreadable reply from the backend: {error}"))?;
                    if value["id"] != json!(id) {
                        drop_process(&mut connection);
                        return Err("the backend answered a different request".into());
                    }
                    Ok(value)
                }
            };
        }
        unreachable!("the second attempt always returns")
    }
}

fn drop_process(connection: &mut Connection) {
    if let Some(mut process) = connection.process.take() {
        let _ = process.child.kill();
        let _ = process.child.wait();
    }
}

#[tauri::command]
async fn backend(app: AppHandle, op: String, args: Option<Value>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.state::<Backend>()
            .request(&app, &op, args.unwrap_or_else(|| json!({})))
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
fn check_now(wake: tauri::State<'_, Wake>) {
    let _ = wake.0.send(());
}

fn show(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn notify(app: &AppHandle, body: &str) {
    let _ = app
        .notification()
        .builder()
        .title("Mashu")
        .body(body)
        .show();
}

fn set_tooltip(app: &AppHandle, text: &str) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(text));
    }
}

/// What the previous poll saw, so only what is new raises a notice.
#[derive(Default)]
struct Seen {
    pending: Option<u64>,
    changes: Option<u64>,
    proposals: Option<HashSet<String>>,
}

fn check(app: &AppHandle, seen: &mut Seen) {
    let reply = match app.state::<Backend>().request(app, "status", json!({})) {
        Ok(reply) if reply["ok"] == json!(true) => reply,
        Ok(reply) => {
            let error = reply["error"].as_str().unwrap_or("unknown error");
            set_tooltip(app, &format!("Mashu: {error}"));
            return;
        }
        Err(error) => {
            set_tooltip(app, &format!("Mashu: {error}"));
            return;
        }
    };
    let status = &reply["result"];
    let pending = status["pending_ready"].as_u64().unwrap_or(0);
    let changes = status["memory_changes"].as_u64().unwrap_or(0);
    let proposals: Vec<&Value> = status["close_proposals"]
        .as_array()
        .map(|rows| rows.iter().collect())
        .unwrap_or_default();
    set_tooltip(
        app,
        &format!(
            "Mashu: {pending} waiting for review, {changes} Memory change(s), {} close proposal(s)",
            proposals.len()
        ),
    );

    match seen.changes {
        None if changes > 0 => notify(app, &format!("{changes} Memory change(s) proposed")),
        Some(before) if changes > before => notify(
            app,
            &format!(
                "{} new Memory change(s); {changes} proposed",
                changes - before
            ),
        ),
        _ => {}
    }
    seen.changes = Some(changes);

    match seen.pending {
        None if pending > 0 => notify(app, &format!("{pending} candidate(s) waiting for review")),
        Some(before) if pending > before => notify(
            app,
            &format!(
                "{} new candidate(s); {pending} waiting for review",
                pending - before
            ),
        ),
        _ => {}
    }
    seen.pending = Some(pending);

    let ids: HashSet<String> = proposals
        .iter()
        .filter_map(|row| row["task_id"].as_str().map(str::to_owned))
        .collect();
    match &seen.proposals {
        None if !ids.is_empty() => {
            notify(app, &format!("{} task(s) proposed for closing", ids.len()))
        }
        Some(before) => {
            for row in &proposals {
                let id = row["task_id"].as_str().unwrap_or_default();
                if !before.contains(id) {
                    notify(
                        app,
                        &format!(
                            "Close proposed: {} ({})",
                            row["name"].as_str().unwrap_or("?"),
                            row["outcome"].as_str().unwrap_or("?")
                        ),
                    );
                }
            }
        }
        _ => {}
    }
    seen.proposals = Some(ids);

    let _ = app.emit("status", status.clone());
}

fn watch(app: AppHandle, wakeup: Receiver<()>) {
    let mut seen = Seen::default();
    loop {
        check(&app, &mut seen);
        match wakeup.recv_timeout(POLL) {
            Ok(()) | Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
    }
}

fn main() {
    let (wake, wakeup) = channel::<()>();
    let wake_from_tray = wake.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Backend::default())
        .manage(Wake(wake))
        .invoke_handler(tauri::generate_handler![
            backend,
            check_now,
            update::checkout_get,
            update::checkout_set,
            update::update_check,
            update::update_install
        ])
        .setup(move |app| {
            let open = MenuItem::with_id(app, "open", "Open Mashu", true, None::<&str>)?;
            let check = MenuItem::with_id(app, "check", "Check now", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &check, &quit])?;
            let mut tray = TrayIconBuilder::with_id(TRAY_ID)
                .tooltip("Mashu")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "open" => show(app),
                    "check" => {
                        let _ = wake_from_tray.send(());
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            let handle = app.handle().clone();
            thread::spawn(move || watch(handle, wakeup));
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window leaves the app in the tray; Quit in its menu ends it.
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Mashu");
}
