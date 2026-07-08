"""confirmed-ctl — newspaper ad receipt collection + final reconciliation.

Picks up where plaid-ctl leaves off: collects the Gmail receipt PDF, archives it
to Dropbox, and writes the final ``Done`` status back to the CRM.
"""

__version__ = "0.1.0"
