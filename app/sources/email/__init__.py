# Email sources — future implementation
#
# Each email source will implement DataSource and expose tools:
#   - search_emails(query, date_range, sender)
#   - get_thread(thread_id)
#   - list_folders()
