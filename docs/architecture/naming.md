# Naming map: from the clinic base to Next Gen HRMS

The codebase started as a clinic product. Everything we keep is renamed to HR terms, and everything clinic-only is removed. This map is the single reference for the database, the backend and the frontend.

## Database tables

| Old (clinic) | New (HRMS) | Notes |
|---|---|---|
| `businesses` | `companies` | The tenant. `business_name` → `company_name`. Booking columns removed (`booking_mode`, `parallel_capacity`, `default_slot_duration`, `buffer_minutes`, `advance_booking_days`, `min_cancellation_hours`). Type/category links removed. New columns: `industry`, `employee_count_band`, `pan`, `tan`, `gstin`, `cin`, `pincode`. The owner login (`email`, `password`) stays on the company as today. |
| `business_types`, `business_sub_categories`, `subcategory_config` | removed | Industry is a plain field on the company. |
| `users` | `employees` | `business_id` → `company_id`. New `employee_code`. Clinic fields removed (`registration_number`). |
| `user_data` | `employee_settings` | `user_id` → `employee_id`. Holds work arrangement (working days and shift timings). |
| `user_services` | removed | |
| `roster` | `roster_entries` | `user_id` → `employee_id`. |
| `departments` | `departments` | `business_id` → `company_id`, `head_doctor_id` → `head_employee_id`. |
| `designations` | `designations` | `business_id` → `company_id`. |
| `roles` | `roles` | `business_id` → `company_id`. System roles: `admin` (HR admin), `hr`, `manager`, `employee`. Permissions are HR module permissions. |
| `audit_log` | `audit_logs` | `business_id` → `company_id`, user → `employee_id`. |
| `notifications` | `notifications` | `business_id` → `company_id`. |
| `whatsapp_connections` | `whatsapp_connections` | `business_id` → `company_id` (kept for WhatsApp self-service later). |
| `platform_users`, `platform_settings`, `subscription_payments`, `payment_codes`, `commissions`, `payouts` | same names | `business_id` → `company_id`. Plan ids `clinic` / `clinic_whatsapp` → `standard` / `standard_whatsapp`. |
| Everything else (appointments, customers, patients, consultations, prescriptions, medicines, investigations, wards, beds, admissions, OT, procedures, treatment plans, insurance, invoices, payments, purchase orders, suppliers, services, equipment, certificates, consent forms, booking bots, vitals, progress notes, patient documents) | removed | |

## API addresses (`/api/v1/...`)

| Old | New |
|---|---|
| `/business/...` | `/company/...` |
| `/staff/...` | `/employees/...` |
| `/public/site/{slug}` (subdomain check) | `/public/companies/{slug}` |
| `/admin/clinics...` | `/admin/companies...` |
| `/me/clinics` (partner) | `/me/companies` |
| `/departments`, `/designations`, `/roster`, `/roles`, `/audit`, `/notifications`, `/auth`, `/settings`, `/upi`, `/platform...` | same, with HR wording inside |

## Fields in responses and tokens

| Old | New |
|---|---|
| `business_id` | `company_id` |
| `business_name` | `company_name` |
| `doctor_user_id` | removed |
| `head_doctor_id`, `head_doctor_name` | `head_employee_id`, `head_employee_name` |
| `clinics`, `total_clinics`, `price_clinic` | `companies`, `total_companies`, `price_standard` |
| Header `X-Clinic-Slug` | `X-Company-Slug` |
| Error "Token does not belong to this clinic" | "Token does not belong to this company" |

## Browser storage keys

| Old | New |
|---|---|
| `bookeasy_token`, `bookeasy_admin_token` | `hrms_token`, `hrms_admin_token` |
| `clineasy_*`, `clinimate_*`, `nextgenhrms_*` leftovers | `hrms_*` |

## Development data

- Platform admin: `admin@nextgenhrms.dev` / `Platform9#admin`
- Demo company owner: `tintu@test.com` / `Welcome9#` (web address `nextgenhrms-admin`)
- No clinic seed data (no clinic test companies, medical departments or designations).
