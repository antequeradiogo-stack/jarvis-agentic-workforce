"""Task-text policy: task text is DATA, never authority. Default-deny classifier (ported and generalised from an
earlier private prototype; personal and channel-specific strings removed).

A task goal is acceptable for autonomous work ONLY if (a) it matches at least one allow-listed SAFE kind (research,
summarise, draft-for-review, read-only analysis, study, internal planning) AND (b) no consequential, indirect-delegation
or injection pattern matches on ANY normalised variant of the text. Deny wins over allow. Languages: EN / PT / NO.

Normalisation variants: NFKC + casefold + diacritics stripped + zero-width removed + Cyrillic/Greek homoglyphs mapped
to Latin; 'despaced' (s e n d / s.e.n.d -> send); 'leet' (0->o 1->i 3->e 4->a 5->s 7->t @->a $->s); 'squashed' for
long, low-ambiguity deny stems. Consequential actions are requested through the structured ``deliver`` field and the
gate, never through free text. Limits: regex-based; it will have false positives (by design) and can miss novel
phrasings, which is why the gate, not this classifier, is the security boundary."""
import re, unicodedata

HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ѕ": "s", "к": "k", "м": "m",
    "т": "t", "н": "h", "в": "b", "ј": "j", "ԁ": "d", "ɡ": "g", "ӏ": "l", "ο": "o", "α": "a", "ε": "e", "ρ": "p",
    "ν": "v", "τ": "t", "κ": "k", "ι": "i", "υ": "u", "χ": "x", "ø": "o", "æ": "ae", "å": "a", "ß": "ss", "ł": "l"})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "l"})

SAFE_KINDS = {
    "research": r"\b(research|investigate|look ?up|find (out|info)|compare|pesquis|investig|undersøk|undersok|finn ut)",
    "summarize": r"\b(summari[sz]e|summary|tl;?dr|digest|overview|recap|brief\b|resum|oppsummer|sammendrag)",
    "draft_for_review": r"\b(draft|outline|write (a |an )?(note|doc|markdown|outline|memo|report|plan)|rascunho|esboço|esboco|utkast)",
    "read_only_analysis": r"\b(analy[sz]e|analysis|review|audit|assess|evaluate|explain|read[- ]only|inspect|analis|avalia|analyser|vurder)",
    "study_learning": r"\b(study|learn|quiz|flashcard|exam prep|practice questions|teach me|estud|aprend|lær|laer|øv|ov på)",
    "internal_planning": r"\b(plan|prioriti[sz]e|roadmap|schedule a study|checklist|todo list|backlog|planear|planeje|planlegg|prioriter)",
}

DENY = {  # category -> pattern (word-start anchored; EN / PT / NO)
    "email_or_message_send": r"\b(send|sent|mail|e-?mail|e-?post|epost|message|msg|text(ing)? (him|her|them)|sms|dm\b|direct message|ping|tell|inform|notify|"
                             r"reply|respond|answer (the|him|her|them)|forward|contact|reach out|call|phone|ring (opp|til)|whatsapp|slack|telegram|signal\b|"
                             r"linkedin|enviar|envia|envie|mandar|manda|responder|responde|encaminh|contactar|contacta|ligar|liga |telefon|mensagem|correio|"
                             r"sende|svar|videresend|kontakt|melding)",
    "job_application": r"\b(apply|application|submit (my |the )?(cv|resume|application|form)|candidat|søk på|sok pa|søknad|soknad|register for)",
    "publishing": r"\b(publish|tweet|post(ing)? (it|this|on|to)|post\b|push|deploy|release|go live|public|upload|share|merge|commit|open source|"
                  r"create (a |the )?(github |git )?repo|publicar|partilh|compartilh|publiser|del (den|det)|legg ut)",
    "spending": r"(\b(buy|purchase|pay|order|subscribe|book|checkout|spend|invoice|top[- ]?up|credit card|card|billing|wire|transfer|iban|swift|"
                r"fund|money|donat|crypto|bitcoin|voucher|comprar|compra|pagar|paga|transfer[eê]|transferencia|encomend|reservar|"
                r"kjøp|kjop|betal|overfør|overfor|bestill)|[$€£]\s?\d|\d\s?(eur|usd|nok|kr)\b)",
    "agreements_commitments": r"\b(accept|decline|reject|confirm|rsvp|agree|sign\b|sign (the|it|this)|signature|contract|offer|commit to|"
                              r"aceit|recus|assin|contrato|aksepter|godta|signer|kontrakt|tilbud)",
    "calendar": r"\b(calendar|invite|meeting|schedule (a |the )?(call|meeting|interview)|reschedule|cancel|agenda|convite|reuni|møte|mote|kalender)",
    "account_change": r"\b(sign ?up|log ?in|login|account|settings|plan change|change (my )?(plan|password)|unsubscribe|enable routine|"
                      r"conta\b|inscrev|registar|konto|meld (meg )?på)",
    "credentials": r"\b(password|passwd|pass ?phrase|api[ _-]?key|token|secret|credential|2fa|otp|ssh key|private key|senha|palavra-passe|"
                   r"chave|passord|nøkkel|nokkel)",
    "destructive_or_user_files": r"(\b(delete|remove|rm\b|rm -|wipe|erase|overwrite|format|drop (table|database)|truncate|kill|apagar|apaga|"
                                 r"eliminar|elimina|slett|fjern)|\bmodify|\bedit (my|the) (files?|repo|pc|computer))",
    "code_execution_or_system": r"\b(run|execute|exec|shell|sudo|chmod|install|script|command|terminal|curl|wget|executar|corre |kjør|kjor)",
    "indirect_delegation": r"(on my behalf|in my name|let (him|her|them|the \w+|\w+) know|get in touch|drop (him|her|them|\w+) a line|follow[- ]?up with|"
                           r"loop (in|\w+ in)|go ahead|make it happen|handle (it|this) for me|take care of it|em meu nome|por mim|på mine vegne|pa mine vegne)",
}
SQUASH_STEMS = ["email", "sendemail", "transfer", "password", "apikey", "publish", "deleteall", "wiretransfer", "creditcard",
                "gitpush", "signthe", "accepttheoffer", "enviar", "overfør", "overfor", "passord"]
INJECTION = [
    r"ignore (all |any |the |your )?(previous |prior |above )?(rules|instructions|policy|policies)",
    r"\byou are (now )?(approved|authori[sz]ed|allowed)\b", r"\b(pre-?)?approved by (the )?(user|owner|admin|manager)\b",
    r"\bapproval[:= ]+(granted|yes|true)\b", r"\bno (need for|need to ask|approval needed)\b",
    r"\b(system|developer) (prompt|message)\s*:", r"\bdo not ask (the user|for confirmation)\b",
    r"\bact as (root|admin|the orchestrator)\b", r"</?(system|assistant|tool)\b", r"\bjailbreak\b", r"\bdisregard\b",
    r"\b(ignora|ignorer) (as |alle )?(regras|instru|regler)", r"\b(aprovado|godkjent)\b",
]

def _fold(t):
    t = unicodedata.normalize("NFKC", t).casefold()
    t = re.sub(r"[\u200b-\u200f\u2060\u00ad\ufeff]", "", t)
    t = t.translate(HOMOGLYPHS)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t)

def _despace(t):  # "s e n d", "s.e.n.d", "s-e-n-d" -> "send"
    return re.sub(r"\b(?:[a-z0-9@$][ .\-_*]){2,}[a-z0-9@$]\b", lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), t)

def variants(text):
    base = _fold(text)
    v = {base, _despace(base), base.translate(LEET), _despace(base.translate(LEET)), _despace(base).translate(LEET)}
    return v

def classify(text: str) -> dict:
    vs = variants(text)
    squashed = {re.sub(r"[^a-z]", "", x) for x in vs}
    deny = sorted({k for k, p in DENY.items() for x in vs if re.search(p, x)}
                  | ({"obfuscated_consequential"} if any(s in q for s in SQUASH_STEMS for q in squashed) else set()))
    inj = sum(1 for p in INJECTION if any(re.search(p, x) for x in vs))
    safe = sorted(k for k, p in SAFE_KINDS.items() if any(re.search(p, x) for x in vs))
    reasons = list(deny) + (["injection_marker"] if inj else []) + ([] if safe else ["not_on_safe_allowlist"])
    return {"safe_kinds": safe, "consequential": deny, "injection_markers": inj,
            "requires_user_approval": bool(deny) or bool(inj) or not safe, "reasons": reasons,
            "action_permitted": False}


def intake_check(goal: str) -> tuple[bool, str]:
    """Orchestrator intake hook: (ok, reason). ok=False means the task is rejected before any agent runs."""
    c = classify(goal)
    if c["requires_user_approval"]:
        return False, "goal text not acceptable for autonomous work: " + ", ".join(c["reasons"])
    return True, "safe kinds: " + ", ".join(c["safe_kinds"])
