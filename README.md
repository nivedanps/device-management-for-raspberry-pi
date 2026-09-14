%-------------------------
% Resume in Latex
% Based off of: https://github.com/sb2nov/resume (Jake Gutierrez template)
%------------------------

\documentclass[letterpaper,11pt]{article}

\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[colorlinks=true, linkcolor=blue, urlcolor=blue, citecolor=blue]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}
\usepackage{multicol}
\setlength{\multicolsep}{-3.0pt}
\setlength{\columnsep}{-1pt}
\input{glyphtounicode}

\pagestyle{fancy}
\fancyhf{} % clear all header and footer fields
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}

% Adjust margins
\addtolength{\oddsidemargin}{-0.6in}
\addtolength{\evensidemargin}{-0.5in}
\addtolength{\textwidth}{1.19in}
\addtolength{\topmargin}{-.7in}
\addtolength{\textheight}{1.4in}

\urlstyle{same}

\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}

% Sections formatting
\titleformat{\section}{
  \vspace{-4pt}\scshape\raggedright\large\bfseries
}{}{0em}{}[\color{black}\titlerule \vspace{-5pt}]

% Ensure that generated pdf is machine readable/ATS parsable
\pdfgentounicode=1

%-------------------------
% Custom commands
\newcommand{\resumeItem}[1]{
  \item\small{
    {#1 \vspace{-2pt}}
  }
}

\newcommand{\resumeCompactItem}[1]{
  \item[] \small #1
}

\newcommand{\resumeSubheading}[4]{
  \vspace{-2pt}\item
    \begin{tabular*}{1.0\textwidth}[t]{l@{\extracolsep{\fill}}r}
      \textbf{#1} & \textbf{\small #2} \\
      \textit{\small#3} & \textit{\small #4} \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeProjectHeading}[2]{
    \item
    \begin{tabular*}{1.001\textwidth}{l@{\extracolsep{\fill}}r}
      \small#1 & \textbf{\small #2}\\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeSubItem}[1]{\resumeItem{#1}\vspace{-4pt}}

\renewcommand\labelitemi{$\vcenter{\hbox{\tiny$\bullet$}}$}
\renewcommand\labelitemii{$\vcenter{\hbox{\tiny$\bullet$}}$}

\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.0in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-5pt}}

%-------------------------------------------
%%%%%%  RESUME STARTS HERE  %%%%%%%%%%%%%%%%%%%%%%%%%%%%

\begin{document}

%----------HEADING----------
\begin{center}
    {\Huge \scshape Nivedan P S} \\ \vspace{4pt}
    \small \Mobilefone\ +91 9740130224 ~ $|$ ~
    \Letter\ \href{mailto:nivedanps1234@gmail.com}{nivedanps1234@gmail.com} ~ $|$ ~
    \href{https://linkedin.com/in/nivedanps}{linkedin} ~ $|$ ~
    \href{https://nivedanpsportfolio.vercel.app/}{portfolio} ~ $|$ ~
    \href{https://github.com/nivedanps}{github}
    \vspace{-8pt}
\end{center}

%-----------SUMMARY-----------
\section{Summary}
  \resumeSubHeadingListStart
    \resumeItem{Final-year Computer Science undergraduate with hands-on project experience in AI-driven and web-based applications. Strong foundation in Python, Java, and database systems, with a growing interest in generative AI tools and cloud technologies. Quick learner, collaborative team member, and enthusiastic open-source contributor.}
  \resumeSubHeadingListEnd
\vspace{-13pt}

%-----------EDUCATION-----------
\section{Education}
  \resumeSubHeadingListStart
    \resumeSubheading
      {Maharaja Institute of Technology, Mysuru}{2023 -- 2027}
      {Bachelor of Engineering, Computer Science -- CGPA: 8.73}{Mysuru, India}
    \resumeSubheading
      {Sadvidya Semi-Residential College, Mysuru}{2022 -- 2023}
      {Pre-University (PCMC) -- 84.16\%}{Mysuru, India}
    \resumeSubheading
      {Bharatiya Vidya Bhavan, Mysuru}{2020 -- 2021}
      {High School (SSLC) -- 91.20\%}{Mysuru, India}
  \resumeSubHeadingListEnd
\vspace{-13pt}

%-----------PROJECTS-----------
\section{Projects}
    \resumeSubHeadingListStart
      \resumeProjectHeading
          {\textbf{\href{https://github.com/nivedanps/voxsynth}{Voxsynth}} $|$ \emph{Python, AI Agents, Text-to-Speech}}{Feb 2026}
          \resumeItemListStart
            \resumeItem{Built an autonomous multiplayer agent system that translates text to audio in real time across multiple languages simultaneously.}
            \resumeItem{Designed the agent pipeline to coordinate translation and speech synthesis with minimal latency for a seamless multiplayer experience.}
          \resumeItemListEnd
          \vspace{-13pt}
      \resumeProjectHeading
          {\textbf{\href{https://github.com/nivedanps/Smart-feedback-faculty-portal}{Smart Faculty Feedback Portal}} $|$ \emph{ JavaScript, Web Development, Data Visualization}}{Jan 2026}
          \resumeItemListStart
            \resumeItem{Designed a web application to simplify collection and reporting of faculty feedback for academic institutions.}
            \resumeItem{Enhanced decision-making for administrators through organized data visualization and analytics dashboards.}
          \resumeItemListEnd
          \vspace{-13pt}
      \resumeProjectHeading
          {\textbf{\href{https://github.com/nivedanps/zoomanager}{ZOO Database Management System}} $|$ \emph{ JavaScript, MySQL, Database Design}}{Dec 2025}
          \resumeItemListStart
            \resumeItem{Developed a database-driven system that simplifies animal records, feeding schedules, and staff management for a zoo.}
            \resumeItem{Structured the database schema to streamline wildlife care administration and enable smarter, tech-driven operations.}
          \resumeItemListEnd
    \resumeSubHeadingListEnd
\vspace{-15pt}

%-----------TECHNICAL SKILLS-----------
\section{Technical Skills}
 \begin{itemize}[leftmargin=0.15in, label={}]
    \small{\item{
     \textbf{Programming Languages}{: Python, C, Java} \\
     \textbf{Technologies/Environment}{: MySQL, Git \& GitHub, Jupyter Notebook, VS Code, Antigravity, Web Hosting} \\
     \textbf{Soft Skills}{: Mentoring, Problem Solving, Team Collaboration} \\
    }}
 \end{itemize}
 \vspace{-16pt}

%-----------CERTIFICATIONS \& COURSES-----------
\section{Certifications \& Courses}
  \begin{itemize}[leftmargin=0.15in, label={}, itemsep=-2pt]
    \resumeCompactItem{\textbf{Introduction to Generative AI} -- 2026}
    \resumeCompactItem{\textbf{Artificial Intelligence Fundamentals} -- 2025}
    \resumeCompactItem{\textbf{AWS Cloud Practitioner Essentials} -- 2026}
    \resumeCompactItem{\textbf{Programming with JavaScript} -- 2026}
    \resumeCompactItem{\textbf{Certified in Java Language} -- Acube Tech Skills, Mysuru, India}
  \end{itemize}
\vspace{-10pt}

%-----------ACHIEVEMENTS \& INVOLVEMENT-----------
\section{Hackathons \& Achievements}
  \begin{itemize}[leftmargin=0.15in, label={}, itemsep=-2pt]
    \resumeCompactItem{\textbf{Innovostava 2026} -- Runner-up}
    \resumeCompactItem{\textbf{Hackverse 2025} -- Participant}
    \resumeCompactItem{\textbf{ARGHYA -- Empowering Engineers with Next-Gen AI Tools} -- Participant}
    \resumeCompactItem{\textbf{Be10x AI Tools Workshop} -- Attendee}
  \end{itemize}
\vspace{-10pt}

%-----------LANGUAGES \& INTERESTS-----------
\section{Languages \& Interests}
 \begin{itemize}[leftmargin=0.15in, label={}]
    \small{\item{
     \textbf{Languages}{: English (Professional), Kannada (Native), Hindi (Limited Working)} \\
     \textbf{Interests}{: Cricket, Kabaddi, Open Source Contribution} \\
    }}
 \end{itemize}

\end{document}
