# Company Knowledge

This file teaches OptiFlow AI about your business. It is included in every
SQL generation prompt so the AI writes better, more context-aware queries.

Complete this file during setup (Step 4) or edit it later at /admin/company.

---

## Business Overview

<!-- What does your company do? What industry are you in? -->
<!-- Example: "We are a software services company. We manage projects, invoices, and AMC contracts for enterprise clients." -->


## Key Tables and What They Mean

<!-- Explain what your most important tables contain and how they relate. -->
<!-- Example:
- Orders table: customer purchase orders, each row is one order line
- Customers table: master list of all client companies
- Products table: our product catalog
-->


## Terminology

<!-- Define any business terms or column values that need explanation. -->
<!-- Example:
- "FOC" means "Free of Charge" (zero-value invoice)
- "PIC" = Person In Charge (customer-side contact, not our staff)
- Project status values: Seed (lead), Root (quoted), Ground (PO received), Plant (completed)
-->


## Data Quality Rules

<!-- Any filters that should ALWAYS be applied to avoid dirty/test data. -->
<!-- Example:
- The Orders table has test orders created on 2024-01-01 — always exclude them with: Created_Date != '2024-01-01'
- The Projects table includes cancelled projects with Status = 'Cancelled' — exclude unless specifically asked
-->


## Important Relationships

<!-- How do key tables join together? -->
<!-- Example:
- Orders JOIN Customers ON Orders.CustomerCode = Customers.Code
- Projects JOIN Invoices ON Projects.ProjectCode = Invoices.ProjectCode
-->
