//! Background task management.
//!
//! In Rust with tokio, spawned tasks are tracked by the runtime and won't
//! be dropped prematurely. This module provides a JoinSet-based tracker
//! for structured cleanup and error logging.

use std::future::Future;
use std::sync::Arc;

use tokio::task::JoinSet;
use tokio::sync::Mutex;

pub struct BackgroundTaskSet {
    tasks: Arc<Mutex<JoinSet<()>>>,
}

impl BackgroundTaskSet {
    pub fn new() -> Self {
        Self {
            tasks: Arc::new(Mutex::new(JoinSet::new())),
        }
    }

    /// Spawn a fire-and-forget task that will be cleaned up on drop.
    pub async fn fire_and_forget<F>(&self, future: F)
    where
        F: Future<Output = ()> + Send + 'static,
    {
        let mut tasks = self.tasks.lock().await;
        tasks.spawn(future);
    }

    pub async fn len(&self) -> usize {
        let tasks = self.tasks.lock().await;
        tasks.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.len().await == 0
    }

    /// Abort all running tasks.
    pub async fn abort_all(&self) {
        let mut tasks = self.tasks.lock().await;
        tasks.abort_all();
    }
}

impl Default for BackgroundTaskSet {
    fn default() -> Self {
        Self::new()
    }
}
