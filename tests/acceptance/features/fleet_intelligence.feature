Feature: Connected-vehicle intelligence end to end
  As a fleet manager I want faults and risky behaviour detected from live telemetry,
  explained with evidence and turned into recommended actions, without seeing other tenants' data.

  Scenario: Engine failure is detected, explained and recommended
    Given I am signed in as "fleet_manager@acme.demo"
    When the "engine_failure" scenario is injected and the pipeline has processed it
    Then an open "critical" alert of type "engine_overheating" exists for the scenario vehicle
    And the scenario vehicle health score is below 50
    When I ask the copilot "Explain why the vehicle is unhealthy"
    Then the answer mentions "overheating"
    And the copilot cites the tools "get_vehicle_health" and "search_telemetry"
    And the copilot recommends at least one action

  Scenario: Dangerous driver raises driver risk
    Given I am signed in as "fleet_manager@acme.demo"
    When the "dangerous_driver" scenario is injected and the pipeline has processed it
    Then an open "critical" alert of type "dangerous_driving" exists for the scenario vehicle
    When I ask the copilot "Which drivers are dangerous?"
    Then the answer lists the scenario vehicle

  Scenario: EV battery degradation produces a maintenance recommendation
    Given I am signed in as "fleet_manager@borealis.demo"
    When the "ev_battery" scenario is injected and the pipeline has processed it
    Then an open "critical" alert of type "battery_degradation" exists for the scenario vehicle
    When I ask the copilot "Show vehicles with abnormal battery degradation."
    Then the answer lists the scenario vehicle
    And the copilot recommends at least one action

  Scenario: Fleet idling is costed at fleet level
    Given I am signed in as "fleet_manager@metro.demo"
    When the "fleet_idling" scenario is injected and the pipeline has processed it
    Then at least 6 vehicles have an open "excessive_idling" alert
    When I ask the copilot "How much money could we save by reducing idling?"
    Then the answer mentions "USD"
    And the copilot cites the tools "calculate_idle_cost"

  Scenario: Duplicate and out-of-order deliveries do not double count
    Given I am signed in as "fleet_manager@acme.demo"
    When every event of the "dangerous_driver" scenario is delivered three times in random order
    Then each distinct event is counted exactly once

  Scenario: Tenants are isolated
    Given I am signed in as "admin@acme.demo"
    And I note a vehicle belonging to tenant "borealis"
    Then fetching that vehicle returns 404
    And the copilot refuses to discuss it

  Scenario: Risky agent actions need explicit confirmation
    Given I am signed in as "fleet_manager@acme.demo"
    When I ask the copilot to create an alert for one of my vehicles
    Then no alert is created until I confirm
    And confirming creates exactly one alert
