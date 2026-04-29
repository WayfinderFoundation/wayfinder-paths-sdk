use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use axum::body::Body;
use axum::extract::State;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use clap::Parser;
use dashmap::DashMap;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::process::{Child, Command};
use tokio::sync::{oneshot, Mutex};
use tokio::time::sleep;
use tracing::{error, info, warn};
use uuid::Uuid;

const PROTOCOL_VERSION: &str = "2024-11-05";
const SERVER_NAME: &str = "wayfinder";
const SERVER_VERSION: &str = "1.0.0";

#[derive(Parser, Debug, Clone)]
#[command(version, about = "Wayfinder MCP Rust frontend")]
struct Args {
    #[arg(long)]
    manifest: PathBuf,
    #[arg(long)]
    worker_script: PathBuf,
    #[arg(long)]
    worker_python: PathBuf,
    #[arg(long, default_value = "127.0.0.1:8000")]
    listen: String,
    #[arg(long, default_value = "/tmp/wayfinder-mcp.sock")]
    worker_socket: PathBuf,
}

#[derive(Clone)]
struct AppState {
    manifest: Arc<Value>,
    sessions: Arc<DashMap<String, ()>>,
    worker: Arc<WorkerClient>,
}

struct WorkerClient {
    socket: PathBuf,
    pending: DashMap<String, oneshot::Sender<Value>>,
    writer: Mutex<Option<tokio::net::unix::OwnedWriteHalf>>,
    started: Mutex<bool>,
    worker_python: PathBuf,
    worker_script: PathBuf,
}

impl WorkerClient {
    fn new(socket: PathBuf, worker_python: PathBuf, worker_script: PathBuf) -> Self {
        Self {
            socket,
            pending: DashMap::new(),
            writer: Mutex::new(None),
            started: Mutex::new(false),
            worker_python,
            worker_script,
        }
    }

    async fn ensure_started(self: &Arc<Self>) -> Result<()> {
        let mut started = self.started.lock().await;
        if *started {
            return Ok(());
        }
        // Remove any stale socket from a prior run.
        let _ = tokio::fs::remove_file(&self.socket).await;
        info!("spawning python worker: {} {}", self.worker_python.display(), self.worker_script.display());
        let mut cmd = Command::new(&self.worker_python);
        cmd.arg(&self.worker_script)
            .arg("--socket")
            .arg(&self.socket)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .kill_on_drop(false);
        // Inherit env (OPENCODE_INSTANCE_ID, WAYFINDER_API_KEY, etc.)
        let mut child: Child = cmd.spawn().context("spawn python worker")?;
        // Connect (poll) until the worker has bound the socket.
        let stream = self.connect_when_ready().await?;
        let (read_half, write_half) = stream.into_split();
        *self.writer.lock().await = Some(write_half);
        let me = Arc::clone(self);
        tokio::spawn(async move {
            me.read_loop(read_half).await;
        });
        // Detach the child — we want it to live for the container's lifetime.
        // If it dies we just log; subsequent calls will fail.
        tokio::spawn(async move {
            match child.wait().await {
                Ok(status) => warn!("python worker exited with {}", status),
                Err(e) => warn!("python worker wait error: {}", e),
            }
        });
        *started = true;
        Ok(())
    }

    async fn connect_when_ready(&self) -> Result<UnixStream> {
        // Up to 30s for the worker to bind. Real cold-start is ~3-5s SDK import.
        for _ in 0..600 {
            if let Ok(s) = UnixStream::connect(&self.socket).await {
                return Ok(s);
            }
            sleep(Duration::from_millis(50)).await;
        }
        anyhow::bail!("python worker socket never became ready: {}", self.socket.display())
    }

    async fn read_loop(self: Arc<Self>, read_half: tokio::net::unix::OwnedReadHalf) {
        let mut reader = BufReader::new(read_half);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line).await {
                Ok(0) => {
                    warn!("worker socket closed");
                    break;
                }
                Ok(_) => {}
                Err(e) => {
                    error!("worker socket read err: {}", e);
                    break;
                }
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let msg: Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(e) => {
                    error!("worker sent non-json: {} ({})", trimmed, e);
                    continue;
                }
            };
            let id = match msg.get("id").and_then(|v| v.as_str()) {
                Some(s) => s.to_string(),
                None => {
                    warn!("worker reply missing id: {}", trimmed);
                    continue;
                }
            };
            if let Some((_, tx)) = self.pending.remove(&id) {
                let _ = tx.send(msg);
            }
        }
    }

    async fn call(self: &Arc<Self>, name: &str, arguments: Value) -> Result<Value> {
        self.ensure_started().await?;
        let id = Uuid::new_v4().to_string();
        let (tx, rx) = oneshot::channel();
        self.pending.insert(id.clone(), tx);
        let req = json!({"id": id, "name": name, "arguments": arguments});
        let line = format!("{}\n", serde_json::to_string(&req)?);
        {
            let mut guard = self.writer.lock().await;
            let writer = guard.as_mut().context("worker writer not initialized")?;
            writer.write_all(line.as_bytes()).await?;
            writer.flush().await?;
        }
        // 5 minute timeout — tools like compile_contract can be slow.
        let resp = match tokio::time::timeout(Duration::from_secs(300), rx).await {
            Ok(r) => r.context("worker response channel closed")?,
            Err(_) => {
                self.pending.remove(&id);
                anyhow::bail!("worker call timeout for tool {}", name);
            }
        };
        Ok(resp)
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();
    let args = Args::parse();

    let manifest_bytes = tokio::fs::read(&args.manifest)
        .await
        .with_context(|| format!("read manifest {}", args.manifest.display()))?;
    let manifest: Value = serde_json::from_slice(&manifest_bytes)
        .with_context(|| format!("parse manifest {}", args.manifest.display()))?;

    let worker = Arc::new(WorkerClient::new(
        args.worker_socket.clone(),
        args.worker_python.clone(),
        args.worker_script.clone(),
    ));

    let state = AppState {
        manifest: Arc::new(manifest),
        sessions: Arc::new(DashMap::new()),
        worker: Arc::clone(&worker),
    };

    // Kick off the python worker spawn in the background. Boot-fast path:
    // initialize / tools/list don't need it; it warms up while opencode
    // negotiates handshake.
    let warmup_state = state.clone();
    tokio::spawn(async move {
        if let Err(e) = warmup_state.worker.ensure_started().await {
            error!("worker warmup failed: {}", e);
        } else {
            info!("python worker ready");
        }
    });

    let app = Router::new()
        .route("/mcp", post(handle_mcp).get(handle_mcp_get).delete(handle_mcp_delete))
        .route("/health", get(|| async { "ok" }))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&args.listen)
        .await
        .with_context(|| format!("bind {}", args.listen))?;
    info!("wayfinder-mcp listening on {}", args.listen);
    axum::serve(listener, app).await?;
    Ok(())
}

async fn handle_mcp_get() -> Response {
    // Server -> client SSE stream. opencode doesn't seem to use this for
    // streamable-http POST flows; respond 405 as the spec allows.
    (StatusCode::METHOD_NOT_ALLOWED, "method not allowed").into_response()
}

async fn handle_mcp_delete() -> Response {
    StatusCode::NO_CONTENT.into_response()
}

#[derive(Deserialize)]
struct JsonRpcReq {
    jsonrpc: Option<String>,
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Value,
}

#[derive(Serialize)]
struct JsonRpcOk {
    jsonrpc: &'static str,
    id: Value,
    result: Value,
}

#[derive(Serialize)]
struct JsonRpcErr {
    jsonrpc: &'static str,
    id: Value,
    error: JsonRpcErrBody,
}

#[derive(Serialize)]
struct JsonRpcErrBody {
    code: i32,
    message: String,
}

async fn handle_mcp(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> Response {
    let req: JsonRpcReq = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => return error_response(Value::Null, -32700, format!("parse error: {}", e), None),
    };
    let _ = req.jsonrpc; // jsonrpc field unused; clients always send "2.0"
    let id = req.id.clone().unwrap_or(Value::Null);

    match req.method.as_str() {
        "initialize" => {
            let session_id = Uuid::new_v4().to_string();
            state.sessions.insert(session_id.clone(), ());
            let result = json!({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": false},
                    "resources": {"subscribe": false, "listChanged": false},
                    "prompts": {"listChanged": false},
                    "experimental": {}
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}
            });
            ok_response_with_session(id, result, Some(session_id), &headers)
        }
        "notifications/initialized" => {
            // Notifications: no id in request, no response body.
            // The MCP spec says the server returns 202 Accepted for notifications.
            (StatusCode::ACCEPTED, "").into_response()
        }
        "ping" => ok_response(id, json!({}), &headers),
        "tools/list" => {
            let tools = state.manifest.get("tools").cloned().unwrap_or(json!([]));
            ok_response(id, json!({"tools": tools}), &headers)
        }
        "resources/list" => {
            // We surface resources only via the read_resource tool — empty list here.
            ok_response(id, json!({"resources": []}), &headers)
        }
        "prompts/list" => ok_response(id, json!({"prompts": []}), &headers),
        "tools/call" => {
            let name = match req.params.get("name").and_then(|v| v.as_str()) {
                Some(s) => s.to_string(),
                None => return error_response(id, -32602, "missing tool name".into(), Some(&headers)),
            };
            let arguments = req
                .params
                .get("arguments")
                .cloned()
                .unwrap_or(json!({}));
            match state.worker.call(&name, arguments).await {
                Ok(resp) => {
                    if let Some(err) = resp.get("error") {
                        let text = err.to_string();
                        let result = json!({
                            "content": [{"type": "text", "text": text}],
                            "isError": true
                        });
                        ok_response(id, result, &headers)
                    } else {
                        let inner = resp.get("result").cloned().unwrap_or(Value::Null);
                        // Stringify the inner result. FastMCP returns text-content
                        // wrapped with the JSON-encoded dict.
                        let text = match &inner {
                            Value::String(s) => s.clone(),
                            other => serde_json::to_string(other).unwrap_or_else(|_| "null".into()),
                        };
                        let result = json!({
                            "content": [{"type": "text", "text": text}],
                            "isError": false
                        });
                        ok_response(id, result, &headers)
                    }
                }
                Err(e) => {
                    let result = json!({
                        "content": [{"type": "text", "text": format!("worker error: {}", e)}],
                        "isError": true
                    });
                    ok_response(id, result, &headers)
                }
            }
        }
        other => error_response(id, -32601, format!("method not found: {}", other), Some(&headers)),
    }
}

fn accept_event_stream(headers: &HeaderMap) -> bool {
    headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.contains("text/event-stream"))
        .unwrap_or(false)
}

fn echo_session_id(headers: &HeaderMap) -> Option<String> {
    // Servers (and the MCP spec) accept either casing. axum normalizes to lower.
    headers
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

fn ok_response(id: Value, result: Value, headers: &HeaderMap) -> Response {
    ok_response_with_session(id, result, echo_session_id(headers), headers)
}

fn ok_response_with_session(
    id: Value,
    result: Value,
    session_id: Option<String>,
    headers: &HeaderMap,
) -> Response {
    let body = JsonRpcOk {
        jsonrpc: "2.0",
        id,
        result,
    };
    finalize(serde_json::to_value(&body).unwrap(), session_id, headers)
}

fn error_response(id: Value, code: i32, message: String, headers: Option<&HeaderMap>) -> Response {
    let body = JsonRpcErr {
        jsonrpc: "2.0",
        id,
        error: JsonRpcErrBody { code, message },
    };
    let h = headers.cloned().unwrap_or_default();
    finalize(serde_json::to_value(&body).unwrap(), echo_session_id(&h), &h)
}

fn finalize(payload: Value, session_id: Option<String>, headers: &HeaderMap) -> Response {
    let serialized = serde_json::to_string(&payload).unwrap_or_else(|_| "{}".into());
    if accept_event_stream(headers) {
        // Streamable-HTTP: server replies with a single SSE message.
        let body = format!("event: message\ndata: {}\n\n", serialized);
        let mut resp = Response::new(Body::from(body));
        let h = resp.headers_mut();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("text/event-stream"));
        h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
        if let Some(sid) = session_id {
            if let Ok(v) = HeaderValue::from_str(&sid) {
                h.insert("mcp-session-id", v);
            }
        }
        resp
    } else {
        let mut resp = Response::new(Body::from(serialized));
        let h = resp.headers_mut();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("application/json"));
        if let Some(sid) = session_id {
            if let Ok(v) = HeaderValue::from_str(&sid) {
                h.insert("mcp-session-id", v);
            }
        }
        resp
    }
}

