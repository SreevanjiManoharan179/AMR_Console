"""Grounding calibration pilot: calibrated ambiguity handling over named map locations (offline).

Calibrated ambiguity handling (conformal prediction over map entities). This
package builds the offline calibration used by typed commands:

  registry  -> the approved named locations and their aliases
  token scorer -> a rule-based token-match scorer (no model, no network)
  dataset   -> template-generated labelled commands (seeded)
  conformal -> split conformal calibration and prediction sets
  policies  -> greedy, fixed threshold, conformal set + ask
  evaluate  -> success, help (question) rate, wrong-move rate, coverage

Speculative navigation (a fourth policy: move along the shared path while asking)
is designed, not built. Nothing here moves a robot or talks to ROS.
"""

NONE_LABEL = "<none>"

__all__ = ["NONE_LABEL"]
