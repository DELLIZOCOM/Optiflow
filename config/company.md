## Business Overview

Based on the database structure, this appears to be a **project-based service business** that likely provides IT services, software development, or technical consulting. The company manages client projects from quotation through delivery, handles Annual Maintenance Contracts (AMC), tracks performance, and maintains detailed financial records including invoicing and payments.

*Note: This is an inference based on the database structure. Please update this section with your actual business description.*

## Key Tables and What They Mean

**Core Business Operations:**
- **ProSt** - Main project status tracking table with project codes, titles, customers, and sales amounts
- **OPERATIONS** - Project operational details including start dates (PSD), delivery dates (PDD), and status
- **CLIENT_MASTER** - Customer information including company details, tax IDs, and contact information

**Sales and Quotations:**
- **QUOTATION_MASTER/DETAILS** - Customer quotes with project details, rates, and amounts
- **AMC_QUOTATION_MASTER/DETAILS** - Specialized quotes for Annual Maintenance Contracts

**Purchase Orders and Contracts:**
- **PO_MASTER/DETAILS** - Purchase orders received from clients with GST calculations
- **AMC_MASTER** - Annual Maintenance Contract details with recurring service information

**Financial Management:**
- **INVOICE_DETAILS** - Billing information for completed work
- **payment_information** - Payment tracking including received amounts and pending balances
- **SPL_AND_AMC_PROJECTS** - Special projects and AMC billing details

**Team and Performance:**
- **PIC_MASTER** - Person In Charge (project managers/leads) with department and contact details
- **USER_MASTER** - System users and employees
- **Monthly_Target** - Department-wise revenue targets and achievements
- **PERFORMANCE_EVALUATION/SCORE** - Employee performance tracking (currently unused)

## Important Terminology

- **PIC** - Person In Charge (likely project manager or team lead)
- **AMC** - Annual Maintenance Contract (ongoing service agreements)
- **PSD/PDD** - Likely Project Start Date/Project Delivery Date
- **Project_Code** - Unique identifier linking projects across all systems
- **CGST/SGST** - Central/State Goods and Services Tax (Indian tax system)
- **GSTIN** - GST Identification Number
- **PAN/TAN/CIN** - Indian business registration numbers
- **ProSt** - Project Status (main project tracking table)

## Data Quality Notes

*Please review and update the following based on your actual data:*

**Empty Tables to Investigate:**
- AMC_PO_DETAILS and AMC_PO_MASTER (0 rows) - Are these new features or data migration issues?
- PERFORMANCE_EVALUATION and PERFORMANCE_SCORE (0 rows) - Performance system not yet implemented?
- SKILL_MASTER (0 rows) - Skills tracking planned but not active?

**Data Validation Needed:**
- Check for consistent Project_Code usage across all tables
- Verify customer information completeness in CLIENT_MASTER
- Review payment reconciliation between invoices and payment_information
- Confirm PIC assignments are current and accurate

**Recommended Reviews:**
- Monthly_Target vs actual revenue achievement tracking
- Outstanding payments and aging analysis
- Project status accuracy and completion rates
- AMC renewal tracking and scheduling

*Update this section with your specific data quality findings and business rules.*