from agents.website import WebsiteAgent

class TenderAgent(WebsiteAgent):
    agent_id = "tender"
    responsibility = 'Locate the active RFQ; extract reference, positions, final quantities/units, technical references, documents and Q&A with withdrawals and scoped amendments.'
