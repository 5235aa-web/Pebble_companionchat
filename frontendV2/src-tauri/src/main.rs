// The desktop shell owns the Python backend: it starts it on launch and kills
// it on exit. Everything the backend writes goes to ~/.pebblecompanion, so the
// install directory stays read-only.
//
// Killing the backend reliably takes more than `child.kill()`. PyInstaller's
// executable is a bootloader that immediately spawns the real Python process as
// a child, so killing the bootloader leaves the worker running and holding the
// sidecar file open. That orphan is what makes a later install fail with
// "Error opening file for writing". Three mechanisms guard against it: a job
// object that dies with this process, an explicit tree kill on exit, and a
// sweep of leftovers before spawning.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::ffi::c_void;
use std::net::TcpStream;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent};

const BACKEND_PORT: u16 = 8002;
const BACKEND_EXE: &str = "pebble-backend-x86_64-pc-windows-msvc.exe";

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const HIGH_PRIORITY_CLASS: u32 = 0x0000_0080;


/// Held open for the whole process on purpose. Closing the last handle to a job
/// with KILL_ON_JOB_CLOSE is what kills its members, so this handle must never
/// be closed early: the kernel closing it on process death is the mechanism.
static SIDECAR_JOB: AtomicIsize = AtomicIsize::new(0);

struct Backend(Mutex<Option<Child>>);

/// Append a line to `<data root>/logs/shell.log` and to stderr.
///
/// The packaged shell has no console (`windows_subsystem = "windows"`), so a
/// failed sidecar spawn would otherwise leave no trace anywhere.
fn log_line(msg: &str) {
    use std::io::Write;
    eprintln!("{msg}");
    let home = std::env::var("USERPROFILE").unwrap_or_default();
    if home.is_empty() {
        return;
    }
    let dir = PathBuf::from(home).join(".pebblecompanion").join("logs");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("shell.log"))
    {
        let _ = writeln!(f, "{msg}");
    }
}

#[repr(C)]
#[derive(Default)]
struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    per_process_user_time_limit: i64,
    per_job_user_time_limit: i64,
    limit_flags: u32,
    minimum_working_set_size: usize,
    maximum_working_set_size: usize,
    active_process_limit: u32,
    affinity: usize,
    priority_class: u32,
    scheduling_class: u32,
}

#[repr(C)]
#[derive(Default)]
struct IO_COUNTERS {
    read_operation_count: u64,
    write_operation_count: u64,
    other_operation_count: u64,
    read_transfer_count: u64,
    write_transfer_count: u64,
    other_transfer_count: u64,
}

#[repr(C)]
#[derive(Default)]
struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    basic_limit_information: JOBOBJECT_BASIC_LIMIT_INFORMATION,
    io_info: IO_COUNTERS,
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory_used: usize,
    peak_job_memory_used: usize,
}

const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: u32 = 9;

extern "system" {
    fn SetPriorityClass(handle: isize, priority: u32) -> i32;
    fn CloseHandle(handle: isize) -> i32;
    fn CreateJobObjectW(attrs: *mut c_void, name: *const u16) -> isize;
    fn SetInformationJobObject(job: isize, class: u32, info: *const c_void, len: u32) -> i32;
    fn AssignProcessToJobObject(job: isize, process: isize) -> i32;
}

fn resource_dir(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .resource_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
}

/// Kill a process and everything it spawned.
///
/// `status()` blocks until taskkill returns, so a caller that is about to write
/// over the executable knows the tree is already gone; `spawn()` would only
/// queue the request and return.
fn kill_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid.to_string()])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

unsafe fn create_sidecar_job() -> isize {
    let job = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
    if job == 0 {
        return 0;
    }
    let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
    info.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    let ok = SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        &info as *const _ as *const c_void,
        std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
    );
    if ok == 0 {
        CloseHandle(job);
        return 0;
    }
    SIDECAR_JOB.store(job, Ordering::SeqCst);
    job
}

unsafe fn adopt_into_job(job: isize, child: &Child) {
    if job == 0 {
        return;
    }
    let handle = child.as_raw_handle() as isize;
    if AssignProcessToJobObject(job, handle) == 0 {
        log_line(&format!("[shell] AssignProcessToJobObject failed"));
    }
}


/// Does whatever listens on the port actually serve the RPC socket?
///
/// `/health` alone is not enough: a backend built before the WebSocket bridge
/// answers it and nothing else. A 101 or 400 on `/ws` proves the route exists.
fn port_serves_ws() -> bool {
    use std::io::{Read, Write};
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", BACKEND_PORT)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let request = "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 64];
    let read = stream.read(&mut buf).unwrap_or(0);
    let head = String::from_utf8_lossy(&buf[..read]);
    head.contains(" 101") || head.contains(" 400")
}

/// PID of the process listening on the backend port.
fn port_owner() -> Option<u32> {
    let output = Command::new("netstat")
        .arg("-ano")
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    for line in text.lines() {
        if line.contains(&format!(":{BACKEND_PORT}")) && line.contains("LISTENING") {
            if let Some(pid) = line.split_whitespace().last() {
                return pid.parse().ok();
            }
        }
    }
    None
}

fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    // A backend already listening is left alone. Killing whatever answers on the
    // port would take down the developer's own dev server, and starting a second
    // one cannot work: the port is taken.
    if TcpStream::connect(("127.0.0.1", BACKEND_PORT)).is_ok() {
        if port_serves_ws() {
            log_line(&format!(
                "[shell] port {BACKEND_PORT} served by a current backend, reusing it"
            ));
            return None;
        }
        // A backend that answers /health but not /ws predates the RPC bridge and
        // will never serve this build. It usually survives a hard kill, keeps
        // the port, and every request fails with no visible reason.
        if let Some(pid) = port_owner() {
            log_line(&format!(
                "[shell] killing stale backend holding port {BACKEND_PORT} (pid {pid})"
            ));
            kill_tree(pid);
            std::thread::sleep(Duration::from_millis(900));
        }
    }

    let root = resource_dir(app);
    let exe = root.join(BACKEND_EXE);
    log_line(&format!("[shell] resource dir: {}", root.display()));
    if !exe.exists() {
        log_line(&format!("[shell] backend executable missing: {}", exe.display()));
        return None;
    }

    // The backend's own output is the only place a startup failure explains
    // itself, and CREATE_NO_WINDOW leaves no console to show it in. Route both
    // streams to a file instead of discarding them.
    let backend_log = std::env::var("USERPROFILE").ok().map(|home| {
        PathBuf::from(home)
            .join(".pebblecompanion")
            .join("logs")
            .join("backend.log")
    });
    if let Some(dir) = backend_log.as_ref().and_then(|p| p.parent()) {
        let _ = std::fs::create_dir_all(dir);
    }
    let log_file = backend_log
        .as_ref()
        .and_then(|p| std::fs::File::create(p).ok());

    let mut command = Command::new(&exe);
    match log_file.as_ref().and_then(|f| f.try_clone().ok()) {
        Some(f) => {
            command.stdout(Stdio::from(f));
        }
        None => {
            command.stdout(Stdio::null());
        }
    }
    match log_file {
        Some(f) => {
            command.stderr(Stdio::from(f));
        }
        None => {
            command.stderr(Stdio::null());
        }
    }
    command
        .env("PYTHONUNBUFFERED", "1")
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);

    match command.spawn() {
        Ok(child) => {
            unsafe {
                SetPriorityClass(child.as_raw_handle() as isize, HIGH_PRIORITY_CLASS);
                let job = if SIDECAR_JOB.load(Ordering::SeqCst) == 0 {
                    create_sidecar_job()
                } else {
                    SIDECAR_JOB.load(Ordering::SeqCst)
                };
                adopt_into_job(job, &child);
            }
            log_line(&format!("[shell] backend started (pid {})", child.id()));
            Some(child)
        }
        Err(err) => {
            log_line(&format!("[shell] backend failed to start: {err}"));
            None
        }
    }
}

/// Poll the port instead of sleeping blindly: a cold start varies a lot, and a
/// fixed sleep is either too short or wastes a second on every launch.
fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", BACKEND_PORT)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        // The bundled HTML is embedded in the binary, so a blank window means
        // either the page never loaded or its script threw. This line tells the
        // two apart without a console.
        .on_page_load(|_webview, payload| {
            log_line(&format!(
                "[shell] page load: {:?} {}",
                payload.event(),
                payload.url()
            ));
        })
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let child = spawn_backend(&handle);
                let ready = wait_for_backend(Duration::from_secs(90));
                if ready {
                    log_line(&format!("[shell] backend ready on :{BACKEND_PORT}"));
                } else {
                    log_line(&format!("[shell] backend did not answer on :{BACKEND_PORT}"));
                }
                let state = handle.state::<Backend>();
                *state.0.lock().unwrap() = child;
                // Tells the splash screen it can fade out.
                let _ = handle.emit("backend-ready", ready);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the Tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            // Take the pid out before dropping the state guard: `State` borrows
            // the handle, so a `Child` living past the lock cannot outlive it.
            let pid = {
                let state = handle.state::<Backend>();
                let mut guard = state.0.lock().unwrap();
                guard.take().map(|child| child.id())
            };
            if let Some(pid) = pid {
                log_line(&format!("[shell] killing backend tree pid {pid}"));
                kill_tree(pid);
            }
        }
    });
}
