# Company: Ezee BizFlow Solutions

## Industry & Business Model
Ezee BizFlow Solutions is a technology services company that provides custom software development, system integration, and Annual Maintenance Contract (AMC) services to clients across multiple Indian states. The company operates on a project-based model, delivering solutions from initial quotation through development, implementation, and ongoing maintenance support.

## Core Business Workflow
Lead/Inquiry → Quotation → Purchase Order → Project Development (Seed → Root → Ground → Plant) → Invoice → Payment → AMC Services

## Table Guide

### ProSt (293 rows)
**Purpose:** Main project tracking table that manages the complete project lifecycle from initial inquiry to delivery
**Key columns:** Project_Code (unique identifier), Project_Status (lifecycle stage), Customer, Project_Title, Sales_Amount, PO_No, PO_Amount
**Status values:** 
- Seed: Initial project inquiry/lead
- Root: Project requirements gathering and planning phase
- Ground: Development and testing phase  
- Plant: Implementation and delivery phase
- Held: Project temporarily paused
- Lost: Project not won
- Adjusted: Project scope or terms modified
- NON PREFERRED: [GUESS] Deprioritized projects
**Relationships:** Links to QUOTATION_MASTER via quotation numbers, PO_MASTER via PO_No, CLIENT_MASTER via customer codes
**Use when asked about:** project pipeline, active projects, projects by status, project lifecycle stages, sales pipeline, project delivery dates, overdue projects, project count by customer, revenue pipeline, project profitability, which projects are stuck in development
**Data quality notes:** Many NULL values in GSTIN and State_Code fields, some projects missing PO information

### CLIENT_MASTER (127 rows)
**Purpose:** Customer/client information repository with contact details and tax information
**Key columns:** client_Name, client_Code (3-letter abbreviation), client_GSTIN, client_Address, client_State, client_Status (active/inactive)
**Status values:** client_Status bit field (1=active, 0=inactive)
**Relationships:** Links to ProSt via customer codes, QUOTATION_MASTER via client codes
**Use when asked about:** customer list, client contact information, active customers, customer locations, GST details, customer by state, client onboarding
**Data quality notes:** Most tax fields (PAN, TAN, CIN) are empty, suggesting incomplete customer data entry

### QUOTATION_MASTER (146 rows) & QUOTATION_DETAILS (238 rows)
**Purpose:** Manages quotations sent to clients with line-item details
**Key columns:** quotation_NUMBER, quotation_DATE, quotation_CLIENT_ID_FK, quotation_GRAND_TOTAL, quotation_STATUS
**Status values:** quotation_STATUS bit field (1=active, 0=cancelled)
**Relationships:** Master-detail relationship, links to CLIENT_MASTER via client ID
**Use when asked about:** quote pipeline, quotation status, quote values, pending quotes, quote conversion rates, quotation history, quote approval status
**Data quality notes:** Some quotations have revised totals, indicating quote modifications

### PO_MASTER (94 rows) & PO_DETAILS (143 rows)
**Purpose:** Purchase order management with line items and GST calculations
**Key columns:** PO_No, PO_Date, Project_Title, PO_Total_Amount, ConfirmationType
**Status values:** ConfirmationType: Email, PO (formal purchase order), Verbal (informal confirmation)
**Relationships:** Links to ProSt via PO numbers, connects to invoice generation
**Use when asked about:** purchase orders, PO status, confirmed projects, PO amounts, formal vs verbal confirmations, PO processing
**Data quality notes:** Good data quality with consistent PO numbering

### OPERATIONS (85 rows)
**Purpose:** Tracks project execution phases with planned vs actual dates and turnaround times
**Key columns:** Project_Code, Status, PSD (Project Start Date), PDD (Project Delivery Date), various TAT_* fields for phase durations
**Status values:**
- COC: [GUESS] Certificate of Completion
- Development: Active development phase
- Testing: Quality assurance phase  
- Implementation: Deployment phase
- In Progress: General active status
**Relationships:** Links to ProSt via Project_Code
**Use when asked about:** project execution status, development timeline, testing progress, implementation schedule, project delays, turnaround times, delivery performance
**Data quality notes:** Many NULL dates suggest incomplete tracking of project phases

### INVOICE_DETAILS (147 rows)
**Purpose:** Invoice line items with GST calculations and payment tracking
**Key columns:** Invoice_No, Project_Code, Total_Amount, Line_Status, GST amounts, TAT tracking fields
**Status values:**
- Pending: Not yet invoiced
- Invoiced: Invoice generated and sent
- Payments Closed: Payment received
- FOC: Free of Charge
**Relationships:** Links to ProSt via Project_Code, payment_information via Invoice_No
**Use when asked about:** invoicing status, pending invoices, payment status, revenue recognition, GST reporting, invoice aging, collection status
**Data quality notes:** Good tracking of invoice lifecycle with timestamps

### payment_information (156 rows)
**Purpose:** Payment tracking with bank references and TDS deductions
**Key columns:** Project_Code, Invoice_No, invoiced_amount, amount_received_date, amount, pending_amount, TDS_Deduction
**Relationships:** Links to INVOICE_DETAILS via Invoice_No
**Use when asked about:** payment collection, outstanding amounts, TDS deductions, payment delays, cash flow, collection efficiency
**Data quality notes:** Some payments have reasons for delays or partial payments

### AMC_MASTER (51 rows)
**Purpose:** Annual Maintenance Contract management with coverage periods and charges
**Key columns:** ProjectCode, CustomerName, AMCStartDate, AMCEndDate, AMCCharges, Status, Payment_Status
**Status values:**
- Awaiting PO: AMC proposed, waiting for purchase order
- Under AMC: Active maintenance contract
- Work In Progress: AMC services being delivered
**Relationships:** Links to main projects, separate AMC quotation system
**Use when asked about:** AMC contracts, maintenance revenue, contract renewals, AMC status, recurring revenue, contract expiry dates
**Data quality notes:** Limited data with only 51 contracts, Payment_Status mostly shows "Received"

### SPL_AND_AMC_PROJECTS (108 rows)
**Purpose:** Special projects and AMC billing with detailed invoice tracking
**Key columns:** Similar to INVOICE_DETAILS but includes AMC_Coverage, StartPeriod, EndPeriod, AMC_Percentage
**Status values:** Same as INVOICE_DETAILS (Pending, Invoiced, Payments Closed, Under Review)
**Relationships:** Parallel to main invoicing system for special/AMC projects
**Use when asked about:** AMC billing, special project invoicing, recurring revenue tracking, AMC payment status
**Data quality notes:** Good tracking of AMC periods and coverage

### TICKET_DETAILS (9 rows)
**Purpose:** Internal task/ticket management system for project work
**Key columns:** Ticket_ID, Assigned_To, Task_Title, Priority, Ticket_Status, Date_Of_Delivery
**Status values:**
- In Progress: Active work
- Resolved: Work completed
- Closed: Ticket finalized
**Relationships:** Internal system, may link to projects via context
**Use when asked about:** internal tasks, ticket status, work assignments, task priorities, team workload
**Data quality notes:** Limited usage with only 9 tickets, mostly high priority items

### USER_MASTER (25 rows)
**Purpose:** Employee/user management with department and reporting structure
**Key columns:** Username, Employee_ID (ECZ001-ECZ025), Department, Reporting_To
**Relationships:** Links to various tables via username fields for created_by, assigned_to
**Use when asked about:** employee list, reporting structure, user access, team organization, department structure
**Data quality notes:** Clean employee data with consistent ID numbering

### Monthly_Target (42 rows)
**Purpose:** Department-wise monthly revenue targets and achievement tracking
**Key columns:** TargetAmount, AchievedAmount, Department, CurrentMonth, BacklogAmount
**Use when asked about:** sales targets, department performance, monthly achievements, revenue goals, performance tracking
**Data quality notes:** Tracks both targets and achievements for performance analysis

### PERFORMANCE_EVALUATION (1 row), PERFORMANCE_SCORE (30 rows), PERFORMANCE_SETTINGS (5 rows)
**Purpose:** Employee performance management system with ratings and evaluations
**Key columns:** Score, CategoryID, EmployeeID, Rating categories (Delivery, Quality, Revenue Impact, etc.)
**Status values:** Star ratings 1-5 across different performance categories
**Use when asked about:** employee performance, performance reviews, rating systems, evaluation scores
**Data quality notes:** Limited evaluation data, appears to be newly implemented system

## Key Business Metrics
- **Project Pipeline Value**: Sum of Sales_Amount from ProSt where Status in ('Seed','Root','Ground')
- **Monthly Revenue**: Sum of invoiced amounts from INVOICE_DETAILS by month
- **Collection Efficiency**: (Amount received / Invoiced amount) * 100 from payment_information
- **Project Delivery Performance**: Compare PDD vs actual delivery dates from OPERATIONS
- **AMC Revenue**: Sum of AMCCharges from AMC_MASTER where Status = 'Under AMC'
- **Quote Conversion Rate**: (POs received / Quotations sent) * 100

## Business Terminology
- **Seed/Root/Ground/Plant**: Project lifecycle phases (inquiry → planning → development → delivery)
- **PIC**: Person In Charge (project contact person)
- **AMC**: Annual Maintenance Contract (ongoing support services)
- **TAT**: Turn Around Time (duration metrics for project phases)
- **FOC**: Free of Charge (no-cost services)
- **GSTIN**: Goods and Services Tax Identification Number
- **HSN Code**: Harmonized System of Nomenclature (tax classification)

## Known Data Issues
- Many projects missing GSTIN and State_Code information affecting GST compliance
- Incomplete customer tax details (PAN, TAN, CIN mostly empty)
- Several skill management tables are completely empty (PRIMARY_SKILLS, SECONDARY_SKILLS, etc.)
- Performance evaluation system appears underutilized
- Some date fields have inconsistent NULL patterns suggesting incomplete process tracking

## Fiscal Calendar
Fiscal year: Please fill in — calendar year assumed (based on date patterns showing standard calendar months)