use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::Parser;
use dashmap::DashMap;
use rmcp::model::{
    CallToolRequestParams, CallToolResult, Content, Implementation, ListToolsResult,
    PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::transport::streamable_http_server::{
    session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::process::Command;
use tokio::sync::{oneshot, Mutex};
use tokio::time::sleep;
use tracing::{error, info, warn};
use uuid::Uuid;

#[derive(Parser, Debug)]
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

struct WorkerClient {
    socket: PathBuf,
    pending: DashMap<String, oneshot::Sender<Value>>,
    writer: Mutex<Option<tokio::net::unix::OwnedWriteHalf>>,
    started: Mutex<bool>,
    worker_python: PathBuf,
    worker_script: PathBuf,
}

impl WorkerClient {
    async fn ensure_started(self: &Arc<Self>) -> Result<()> {
        let mut started = self.started.lock().await;
        if *started {
            return Ok(());
        }
        let _ = tokio::fs::remove_file(&self.socket).await;
        info!("spawning python worker: {} {}", self.worker_python.display(), self.worker_script.display());
        let mut child = Command::new(&self.worker_python)
            .arg(&self.worker_script).arg("--socket").arg(&self.socket)
            .stdin(Stdio::null()).stdout(Stdio::inherit()).stderr(Stdio::inherit())
            .kill_on_drop(false).spawn().context("spawn python worker")?;
        let stream = {
            let mut s = None;
            for _ in 0..600 {
                if let Ok(c) = UnixStream::connect(&self.socket).await { s = Some(c); break; }
                sleep(Duration::from_millis(50)).await;
            }
            s.with_context(|| format!("worker socket never ready: {}", self.socket.display()))?
        };
        let (read_half, write_half) = stream.into_split();
        *self.writer.lock().await = Some(write_half);
        let me = Arc::clone(self);
        tokio::spawn(async move { me.read_loop(read_half).await });
        tokio::spawn(async move {
            match child.wait().await {
                Ok(s) => warn!("python worker exited with {}", s),
                Err(e) => warn!("python worker wait error: {}", e),
            }
        });
        *started = true;
        Ok(())
    }

    async fn read_loop(self: Arc<Self>, read_half: tokio::net::unix::OwnedReadHalf) {
        let mut reader = BufReader::new(read_half);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line).await {
                Ok(0) => { warn!("worker socket closed"); break; }
                Ok(_) => {}
                Err(e) => { error!("worker socket read err: {}", e); break; }
            }
            let trimmed = line.trim();
            if trimmed.is_empty() { continue; }
            let msg: Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(e) => { error!("worker sent non-json: {} ({})", trimmed, e); continue; }
            };
            let Some(id) = msg.get("id").and_then(|v| v.as_str()).map(String::from) else {
                warn!("worker reply missing id: {}", trimmed);
                continue;
            };
            if let Some((_, tx)) = self.pending.remove(&id) { let _ = tx.send(msg); }
        }
        // Worker died: reset state so next call() respawns; drop pending senders so
        // in-flight callers fail fast instead of waiting out the 300s timeout.
        *self.writer.lock().await = None;
        *self.started.lock().await = false;
        self.pending.clear();
    }

    async fn call(self: &Arc<Self>, name: &str, arguments: Value) -> Result<Value> {
        self.ensure_started().await?;
        let id = Uuid::new_v4().to_string();
        let (tx, rx) = oneshot::channel();
        self.pending.insert(id.clone(), tx);
        let line = format!("{}\n", json!({"id": id, "name": name, "arguments": arguments}));
        {
            let mut guard = self.writer.lock().await;
            let writer = guard.as_mut().context("worker writer not initialized")?;
            writer.write_all(line.as_bytes()).await?;
            writer.flush().await?;
        }
        match tokio::time::timeout(Duration::from_secs(300), rx).await {
            Ok(r) => r.context("worker response channel closed"),
            Err(_) => { self.pending.remove(&id); anyhow::bail!("worker call timeout for tool {}", name) }
        }
    }
}

#[derive(Clone)]
struct WayfinderHandler {
    tools: Arc<Vec<Tool>>,
    worker: Arc<WorkerClient>,
}

impl ServerHandler for WayfinderHandler {
    fn get_info(&self) -> ServerInfo {
        let mut info = ServerInfo::default();
        info.protocol_version = ProtocolVersion::V_2024_11_05;
        info.capabilities = ServerCapabilities::builder().enable_tools().build();
        info.server_info = Implementation::new("wayfinder", "1.0.0");
        info
    }

    async fn list_tools(&self, _r: Option<PaginatedRequestParams>, _ctx: RequestContext<RoleServer>) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult { tools: (*self.tools).clone(), next_cursor: None, ..Default::default() })
    }

    async fn call_tool(&self, request: CallToolRequestParams, _ctx: RequestContext<RoleServer>) -> Result<CallToolResult, McpError> {
        let arguments = request.arguments.map(Value::Object).unwrap_or(json!({}));
        match self.worker.call(&request.name, arguments).await {
            Ok(resp) => {
                if let Some(err) = resp.get("error") {
                    Ok(CallToolResult::error(vec![Content::text(err.to_string())]))
                } else {
                    let text = match resp.get("result").cloned().unwrap_or(Value::Null) {
                        Value::String(s) => s,
                        other => serde_json::to_string(&other).unwrap_or_else(|_| "null".into()),
                    };
                    Ok(CallToolResult::success(vec![Content::text(text)]))
                }
            }
            Err(e) => Ok(CallToolResult::error(vec![Content::text(format!("worker error: {}", e))])),
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")))
        .init();
    let args = Args::parse();
    if !args.worker_python.is_file() { anyhow::bail!("worker_python not found: {}", args.worker_python.display()); }
    if !args.worker_script.is_file() { anyhow::bail!("worker_script not found: {}", args.worker_script.display()); }

    let manifest: Value = serde_json::from_slice(
        &tokio::fs::read(&args.manifest).await.with_context(|| format!("read manifest {}", args.manifest.display()))?
    ).with_context(|| format!("parse manifest {}", args.manifest.display()))?;
    let tools: Vec<Tool> = serde_json::from_value(
        manifest.get("tools").cloned().context("manifest missing `tools` array")?
    ).context("manifest tools failed to parse as Vec<Tool>")?;

    let worker = Arc::new(WorkerClient {
        socket: args.worker_socket,
        pending: DashMap::new(),
        writer: Mutex::new(None),
        started: Mutex::new(false),
        worker_python: args.worker_python,
        worker_script: args.worker_script,
    });
    // Warm the worker in the background; opencode handshake doesn't need it for
    // initialize/tools/list and the SDK cold-import is the long pole.
    let warmup = Arc::clone(&worker);
    tokio::spawn(async move {
        if let Err(e) = warmup.ensure_started().await { error!("worker warmup failed: {}", e); }
        else { info!("python worker ready"); }
    });

    let handler = WayfinderHandler { tools: Arc::new(tools), worker };
    let mcp = StreamableHttpService::new(
        move || Ok(handler.clone()),
        Arc::new(LocalSessionManager::default()),
        StreamableHttpServerConfig::default().with_allowed_hosts(["localhost", "127.0.0.1", "0.0.0.0"]),
    );
    let app = axum::Router::new()
        .route("/health", axum::routing::get(|| async { "ok" }))
        .nest_service("/mcp", mcp);

    let listener = tokio::net::TcpListener::bind(&args.listen).await
        .with_context(|| format!("bind {}", args.listen))?;
    info!("wayfinder-mcp listening on {}", args.listen);
    axum::serve(listener, app).await?;
    Ok(())
}
