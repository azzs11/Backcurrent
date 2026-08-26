"""
Loads the failed payment batch and figures out which ones actually
need recovery action. Not everything in the batch is worth touching —
opted-out customers and already-resolved payments get filtered out here
before anything expensive happens.
"""

# implementation coming in Phase 1 / Phase 2
