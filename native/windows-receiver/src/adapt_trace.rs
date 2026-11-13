//! ADAPT observations. These stages do not certify Samsung desktop activation.
use serde::Serialize;
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Starting,
    Advertising,
    ConnectionRequest,
    PeerRejected,
    PeerAdmitted,
    WifiDirectGroup,
    RtspConnected,
    VideoNegotiated,
    PlaybackAccepted,
    InputChannelConnected,
    FirstFrameDecoded,
}

#[derive(Debug, Serialize)]
pub struct Trace {
    pub attempt: u64,
    pub attempt_active: bool,
    pub last_stage: Stage,
    pub observed_stages: BTreeSet<Stage>,
}

impl Default for Trace {
    fn default() -> Self {
        Self { attempt: 0, attempt_active: false, last_stage: Stage::Starting,
            observed_stages: BTreeSet::new() }
    }
}

impl Trace {
    pub fn begin_attempt(&mut self) {
        self.attempt += 1;
        self.attempt_active = true;
        self.observed_stages.clear();
        self.record(Stage::ConnectionRequest);
    }

    pub fn record(&mut self, stage: Stage) {
        self.last_stage = stage;
        self.observed_stages.insert(stage);
    }

    pub fn end_attempt(&mut self) { self.attempt_active = false; }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn advertisement_is_not_a_video_or_input_observation() {
        let mut trace = Trace::default();
        trace.record(Stage::Advertising);
        assert_eq!(trace.attempt, 0);
        assert!(!trace.observed_stages.contains(&Stage::FirstFrameDecoded));
        assert!(!trace.observed_stages.contains(&Stage::InputChannelConnected));
    }
    #[test]
    fn new_attempt_does_not_inherit_previous_video_success() {
        let mut trace = Trace::default();
        trace.begin_attempt();
        trace.record(Stage::FirstFrameDecoded);
        trace.end_attempt();
        assert_eq!(trace.last_stage, Stage::FirstFrameDecoded);
        trace.begin_attempt();
        assert_eq!(trace.attempt, 2);
        assert!(!trace.observed_stages.contains(&Stage::FirstFrameDecoded));
        assert_eq!(trace.last_stage, Stage::ConnectionRequest);
    }
    #[test]
    fn decoded_frame_does_not_certify_input_or_samsung_dex() {
        let mut trace = Trace::default();
        trace.begin_attempt();
        trace.record(Stage::FirstFrameDecoded);
        assert!(!trace.observed_stages.contains(&Stage::InputChannelConnected));
        let receipt = serde_json::to_string(&trace).unwrap();
        assert!(!receipt.contains("dex_confirmed"));
        assert!(!receipt.contains("serial"));
    }
}
