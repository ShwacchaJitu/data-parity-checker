from api_client import fetch_patient_metrics
from auth_manager import get_user_entity

# ── Debug config ──────────────────────────────────────────────────────────────
PATIENT_ID = 100000001
PROFILE = "1"   # credential profile, see auth_manager.py
# ─────────────────────────────────────────────────────────────────────────────

print(f"Running as profile {PROFILE} (userId_entity: {get_user_entity(PROFILE)})")
metrics = fetch_patient_metrics(PATIENT_ID, credential_profile=PROFILE)

clinical_ws_dashboard = metrics["clinical_ws_dashboard"]
print("clinical_ws_dashboard", clinical_ws_dashboard)

RHR_dashboard = metrics["RHR_dashboard"]
print("RHR_dashboard", RHR_dashboard)

hrv_val_dashboard = metrics["hrv_val_dashboard"]
print("hrv_val_dashboard", hrv_val_dashboard)

lifestyle_ws_dashboard = metrics["lifestyle_ws_dashboard"]
print("lifestyle_ws_dashboard", lifestyle_ws_dashboard)

steps_dashboard = metrics["steps_dashboard"]
print("steps_dashboard", steps_dashboard)

sedentary_time_dashboard = metrics["sedentary_time_dashboard"]
print("sedentary_time_dashboard", sedentary_time_dashboard)

sleep_dashboard = metrics["sleep_dashboard"]
print("sleep_dashboard", sleep_dashboard)

mvpa_dashboard = metrics["mvpa_dashboard"]
print("mvpa_dashboard", mvpa_dashboard)

processed_food_dashboard = metrics["processed_food_dashboard"]
print("processed_food_dashboard", processed_food_dashboard)

doctor_id_engagement_dashboard = metrics["doctor_id_engagement_dashboard"]
print("doctor_id_engagement_dashboard", doctor_id_engagement_dashboard)

fitbit_device_dashboard = metrics["fitbit_device_dashboard"]
print("fitbit_device_dashboard", fitbit_device_dashboard)

last_sync_timestamp_dashboard = metrics["last_sync_timestamp_dashboard"]
print("last_sync_timestamp_dashboard", last_sync_timestamp_dashboard)

lifestyle_recs_specific_dashboard = metrics["lifestyle_recs_specific_dashboard"]
print("lifestyle_recs_specific_dashboard", lifestyle_recs_specific_dashboard)

if lifestyle_recs_specific_dashboard and str(lifestyle_recs_specific_dashboard).strip():
    lifestyle_recs_selected_dashboard = "Yes"
else:
    lifestyle_recs_selected_dashboard = "No"

print("lifestyle_recs_selected_dashboard", lifestyle_recs_selected_dashboard)


condition_doctor_dashboard = metrics["condition_doctor_dashboard"]
print("condition_doctor_dashboard", condition_doctor_dashboard)

condition_assessment_dashboard = metrics["condition_assessment_dashboard"]
print("condition_assessment_dashboard", condition_assessment_dashboard)

os_date_dashboard = metrics["os_date_dashboard"]
print("os_date_dashboard", os_date_dashboard)

sex_dashboard = metrics["sex_dashboard"]
print("sex_dashboard", sex_dashboard)

race_dashboard = metrics["race_dashboard"]
print("race_dashboard", race_dashboard)

employment_dashboard = metrics["employment_dashboard"]
print("employment_dashboard", employment_dashboard)

age_dashboard = metrics["age_dashboard"]
print("age_dashboard", age_dashboard)

industry_dashboard = metrics["industry_dashboard"]
print("industry_dashboard", industry_dashboard)

work_wellness_prog_dashboard = metrics["work_wellness_prog_dashboard"]
print("work_wellness_prog_dashboard", work_wellness_prog_dashboard)

grocery_shopping_dashboard = metrics["grocery_shopping_dashboard"]
print("grocery_shopping_dashboard", grocery_shopping_dashboard)

eating_out_dashboard = metrics["eating_out_dashboard"]
print("eating_out_dashboard", eating_out_dashboard)

physical_activity_dashboard = metrics["physical_activity_dashboard"]
print("physical_activity_dashboard", physical_activity_dashboard)

condition_self_dashboard = metrics["condition_self_dashboard"]
print("condition_self_dashboard", condition_self_dashboard)

pcc_timestamp_dashboard = metrics["pcc_timestamp_dashboard"]
print("pcc_timestamp_dashboard", pcc_timestamp_dashboard)

pcc_intervention_dashboard = metrics["pcc_intervention_dashboard"]
print("pcc_intervention_dashboard", pcc_intervention_dashboard)

fbg_dashboard = metrics["fbg_dashboard"]
print("fbg_dashboard", fbg_dashboard)

hba1c_dashboard = metrics["hba1c_dashboard"]
print("hba1c_dashboard", hba1c_dashboard)

tchol_dashboard = metrics["tchol_dashboard"]
print("tchol_dashboard", tchol_dashboard)

hdl_dashboard = metrics["hdl_dashboard"]
print("hdl_dashboard", hdl_dashboard)

ldl_dashboard = metrics["ldl_dashboard"]
print("ldl_dashboard", ldl_dashboard)

tg_dashboard = metrics["tg_dashboard"]
print("tg_dashboard", tg_dashboard)

bmi_dashboard = metrics["bmi_dashboard"]
print("bmi_dashboard", bmi_dashboard)

wc_dashboard = metrics["wc_dashboard"]
print("wc_dashboard", wc_dashboard)

smoke_dashboard = metrics["smoke_dashboard"]
print("smoke_dashboard", smoke_dashboard)

alcohol_dashboard = metrics["alcohol_dashboard"]
print("alcohol_dashboard", alcohol_dashboard)

unhealthy_oil_fat_dashboard = metrics["unhealthy_oil_fat_dashboard"]
print("unhealthy_oil_fat_dashboard", unhealthy_oil_fat_dashboard)

fruit_vegetables_dashboard = metrics["fruit_vegetables_dashboard"]
print("fruit_vegetables_dashboard", fruit_vegetables_dashboard)

wholegrains_dashboard = metrics["wholegrains_dashboard"]
print("wholegrains_dashboard", wholegrains_dashboard)

limit_sugar_dashboard = metrics["limit_sugar_dashboard"]
print("limit_sugar_dashboard", limit_sugar_dashboard)

stress_dashboard = metrics["stress_dashboard"]
print("stress_dashboard", stress_dashboard)

clinic_dashboard = metrics["clinic_dashboard"]
print("clinic_dashboard", clinic_dashboard)

assessment_date_dashboard = metrics["assessment_date_dashboard"]
print("assessment_date_dashboard", assessment_date_dashboard)

weight_dashboard = metrics["weight_dashboard"]
print("weight_dashboard", weight_dashboard)

issue_latest_wellness_plan_time_dashboard = metrics["issue_latest_wellness_plan_time_dashboard"]
print("issue_latest_wellness_plan_time_dashboard", issue_latest_wellness_plan_time_dashboard)

sbp_self_dashboard = metrics["sbp_self_dashboard"]
print("sbp_self_dashboard", sbp_self_dashboard)

dbp_self_dashboard = metrics["dbp_self_dashboard"]
print("dbp_self_dashboard", dbp_self_dashboard)

sbp_clinic_dashboard = metrics["sbp_clinic_dashboard"]
print("sbp_clinic_dashboard", sbp_clinic_dashboard)

dbp_clinic_dashboard = metrics["dbp_clinic_dashboard"]
print("dbp_clinic_dashboard", dbp_clinic_dashboard)